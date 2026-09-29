"""Four-agent deep-research workflow: discovery, review, synthesis, hypotheses.

The tool functions run inside sandboxed tool-worker children and only ever touch
``artifacts/<run_id>/`` for the gateway-authenticated run. The orchestrator runs
each stage as an isolated agent process and starts the next stage only after the
previous artifact passes validation.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from .bus import connect
from .harness import HarnessRecord, InMemoryHarnessRecorder, encode_signed_harness_record
from .otel import start_run_span
from .protocol import agent_lifecycle_subject
from .registry import SAFE_TOKEN_RE, ConfigError, load_agent_registry, validate_json
from .supervisor import agent_env


# Resolved at import, in the worker parent, so it survives the sandbox's env scrub.
ARTIFACTS_ROOT = Path(os.getenv("SWARMGUARD_ARTIFACTS_DIR", "artifacts")).resolve()
EXA_SEARCH_URL = "https://api.exa.ai/search"
MAX_EXA_RESPONSE_BYTES = 2_000_000
MIN_CANDIDATES, MAX_CANDIDATES, MAX_INCLUDED = 10, 15, 10
MAX_ABSTRACT_CHARS = 1500

CANDIDATES = "candidates.json"
REVIEWS = "reviews.json"
REVIEWED = "reviewed-papers.md"
SUMMARY = "literature-summary.md"
DIRECTIONS = "future-directions.md"

PAPER_REF_RE = re.compile(r"\[(p-[0-9a-f]{10})\]")

CANDIDATES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["query", "retrieved_at", "raw_response_sha256", "candidates"],
    "properties": {
        "candidates": {
            "type": "array",
            "minItems": MIN_CANDIDATES,
            "maxItems": MAX_CANDIDATES,
            "items": {
                "type": "object",
                "required": ["paper_id", "title", "abstract", "url", "authors", "year", "retrieved_at"],
                "properties": {
                    "paper_id": {"type": "string", "pattern": "^p-[0-9a-f]{10}$"},
                    "title": {"type": "string", "minLength": 1},
                    "abstract": {"type": "string", "minLength": 1},
                    "url": {"type": "string", "pattern": "^https://"},
                    "doi": {"type": ["string", "null"]},
                    "authors": {"type": "array", "items": {"type": "string"}},
                    "year": {"type": ["integer", "null"]},
                    "retrieved_at": {"type": "string"},
                },
            },
        },
    },
}


class ArtifactError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Run-scoped artifact storage
# ---------------------------------------------------------------------------


def run_dir(run_id: str | None) -> Path:
    if not run_id or not SAFE_TOKEN_RE.fullmatch(run_id):
        raise ArtifactError(f"unsafe run_id: {run_id!r}")
    return ARTIFACTS_ROOT / run_id


def read_artifact(run_id: str | None, name: str) -> str:
    path = run_dir(run_id) / name
    if not path.is_file():
        raise ArtifactError(f"{name} has not been produced for this run")
    return path.read_text(encoding="utf-8")


def sha256(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def write_once(run_id: str | None, name: str, text: str) -> dict[str, str]:
    """Atomically create an immutable artifact; an identical rewrite is an idempotent no-op."""
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    temporary = directory / f".{name}.{uuid4().hex}"
    temporary.write_text(text, encoding="utf-8")
    try:
        os.link(temporary, path)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != text:
            raise ArtifactError(f"{name} is immutable and was already written for this run") from None
    finally:
        temporary.unlink()
    return {"artifact": name, "sha256": sha256(text)}


# ---------------------------------------------------------------------------
# Validators shared by the tools and the orchestrator's handoff gate
# ---------------------------------------------------------------------------


def load_candidates(run_id: str | None) -> list[dict[str, Any]]:
    document = json.loads(read_artifact(run_id, CANDIDATES))
    try:
        validate_json(CANDIDATES_SCHEMA, document, label="candidates")
    except ConfigError as exc:
        raise ArtifactError(str(exc)) from exc
    ids = [paper["paper_id"] for paper in document["candidates"]]
    if len(ids) != len(set(ids)):
        raise ArtifactError("candidate paper_ids are not unique")
    return document["candidates"]


def check_reviews(candidates: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidate_ids = {paper["paper_id"] for paper in candidates}
    reviewed_ids = [review["paper_id"] for review in reviews]
    missing = sorted(candidate_ids - set(reviewed_ids))
    unknown = sorted(set(reviewed_ids) - candidate_ids)
    duplicates = sorted({paper_id for paper_id in reviewed_ids if reviewed_ids.count(paper_id) > 1})
    if missing or unknown or duplicates:
        raise ArtifactError(
            f"every candidate needs exactly one decision; missing={missing} unknown={unknown} duplicate={duplicates}"
        )
    if any(not str(review["reason"]).strip() for review in reviews):
        raise ArtifactError("every decision needs a relevance reason")
    included = [review for review in reviews if review["decision"] == "include"]
    if not 1 <= len(included) <= MAX_INCLUDED:
        raise ArtifactError(f"include between 1 and {MAX_INCLUDED} papers; got {len(included)}")
    return included


def load_included(run_id: str | None) -> list[dict[str, Any]]:
    candidates = load_candidates(run_id)
    included = check_reviews(candidates, json.loads(read_artifact(run_id, REVIEWS)))
    reviewed = read_artifact(run_id, REVIEWED)
    by_id = {paper["paper_id"]: paper for paper in candidates}
    for review in included:
        if f"[{review['paper_id']}]" not in reviewed or by_id[review["paper_id"]]["url"] not in reviewed:
            raise ArtifactError(f"{REVIEWED} is missing included paper {review['paper_id']} or its source URL")
    return included


def check_summary(text: str, included_ids: set[str]) -> set[str]:
    if not text.strip():
        raise ArtifactError("summary is empty")
    if re.search(r"\n\s*\n", text.strip()) or re.match(r"\s*([#>*-]|\d+\.)", text):
        raise ArtifactError("summary must be exactly one plain paragraph")
    refs = set(PAPER_REF_RE.findall(text))
    if not refs:
        raise ArtifactError("summary must cite included papers inline as [paper_id]")
    if refs - included_ids:
        raise ArtifactError(f"summary cites papers that were not included: {sorted(refs - included_ids)}")
    return refs


def load_summary_refs(run_id: str | None) -> set[str]:
    included_ids = {review["paper_id"] for review in load_included(run_id)}
    return check_summary(read_artifact(run_id, SUMMARY), included_ids)


def check_directions(run_id: str | None) -> None:
    text = read_artifact(run_id, DIRECTIONS)
    refs = set(PAPER_REF_RE.findall(text))
    allowed = load_summary_refs(run_id)
    if not refs or refs - allowed:
        raise ArtifactError(f"{DIRECTIONS} must cite only papers from the summary; extra={sorted(refs - allowed)}")


# ---------------------------------------------------------------------------
# Exa discovery
# ---------------------------------------------------------------------------


def _norm_doi(doi: Any) -> str | None:
    if not doi:
        return None
    value = str(doi).strip().lower()
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", value) or None


def _norm_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}"


def _norm_title(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", title.lower()).split())


def _abstract_from_text(text: str) -> str:
    marker = "## Abstract"
    return text.split(marker, 1)[1] if marker in text else text


def normalize_exa_results(payload: dict[str, Any], retrieved_at: str) -> list[dict[str, Any]]:
    """Map Exa results to candidate records, dropping unusable entries and duplicates."""
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for result in payload.get("results") or []:
        props = next(
            (entity.get("properties") or {} for entity in result.get("entities") or [] if entity.get("type") == "publication"),
            {},
        )
        title = str(props.get("title") or result.get("title") or "").strip()
        url = str(result.get("url") or "").strip()
        abstract = " ".join(str(props.get("abstract") or _abstract_from_text(str(result.get("text") or ""))).split())
        if not title or not url.startswith("https://") or not abstract:
            continue
        doi = _norm_doi(props.get("doi"))
        keys = {f"url:{_norm_url(url)}", f"title:{_norm_title(title)}"} | ({f"doi:{doi}"} if doi else set())
        if keys & seen:
            continue
        seen |= keys
        authors = [str(author["name"]) for author in props.get("authors") or [] if author.get("name")]
        if not authors and result.get("author"):
            authors = [str(result["author"])]
        year = props.get("year")
        published = str(result.get("publishedDate") or "")
        if not isinstance(year, int):
            year = int(published[:4]) if published[:4].isdigit() else None
        candidates.append(
            {
                "paper_id": "p-" + sha256(doi or _norm_url(url))[:10],
                "title": title,
                "abstract": abstract[:MAX_ABSTRACT_CHARS],
                "url": url,
                "doi": doi,
                "authors": authors[:10],
                "year": year,
                "retrieved_at": retrieved_at,
            }
        )
    return candidates


def _exa_request(query: str, num_results: int, api_key: str) -> bytes:
    body = {
        "query": query,
        "numResults": num_results,
        "type": "auto",
        "category": "research paper",
        "contents": {"text": {"maxCharacters": MAX_ABSTRACT_CHARS}},
    }
    request = urllib.request.Request(
        EXA_SEARCH_URL,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"x-api-key": api_key, "content-type": "application/json", "user-agent": "SwarmGuard/0.1"},
    )
    # The endpoint is fixed: the model controls only the query, never the destination.
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(MAX_EXA_RESPONSE_BYTES + 1)
    if len(raw) > MAX_EXA_RESPONSE_BYTES:
        raise ArtifactError("Exa response exceeds the payload cap")
    return raw


def _search_summary(document: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
    return {
        "artifact": CANDIDATES,
        "replayed": replayed,
        "candidate_count": len(document["candidates"]),
        "raw_response_sha256": document["raw_response_sha256"],
        "candidates": [
            {"paper_id": paper["paper_id"], "title": paper["title"], "year": paper["year"]}
            for paper in document["candidates"]
        ],
    }


def exa_paper_search(arguments: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    path = run_dir(run_id) / CANDIDATES
    if path.is_file():
        # Replay/restart: the run already paid for discovery; never call Exa twice.
        return _search_summary(json.loads(path.read_text(encoding="utf-8")), replayed=True)
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        raise ArtifactError("EXA_API_KEY is not available to the exa_paper_search worker")
    raw = _exa_request(str(arguments["query"]), int(arguments["num_results"]), api_key)
    retrieved_at = datetime.now(timezone.utc).isoformat()
    candidates = normalize_exa_results(json.loads(raw), retrieved_at)
    if len(candidates) < MIN_CANDIDATES:
        raise ArtifactError(
            f"incomplete: Exa returned {len(candidates)} usable papers, need at least {MIN_CANDIDATES}; nothing was stored"
        )
    document = {
        "query": str(arguments["query"]),
        "retrieved_at": retrieved_at,
        "raw_response_sha256": sha256(raw),
        "candidates": candidates[:MAX_CANDIDATES],
    }
    write_once(run_id, CANDIDATES, json.dumps(document, indent=2, sort_keys=True))
    return _search_summary(document, replayed=False)


# ---------------------------------------------------------------------------
# Stage-scoped artifact tools
# ---------------------------------------------------------------------------


def candidate_read(_arguments: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    return {"candidates": load_candidates(run_id)}


def reviewed_papers_write(arguments: dict[str, Any], run_id: str | None) -> dict[str, str]:
    candidates = load_candidates(run_id)
    reviews = list(arguments["reviews"])
    included = check_reviews(candidates, reviews)
    by_id = {paper["paper_id"]: paper for paper in candidates}
    lines = [f"# Reviewed papers ({len(included)} included of {len(candidates)} candidates)", "", "## Included", ""]
    for review in included:
        paper = by_id[review["paper_id"]]
        authors = ", ".join(paper["authors"]) or "unknown authors"
        lines += [
            f"### [{paper['paper_id']}] {paper['title']}",
            f"- Authors: {authors} ({paper['year'] or 'n.d.'})",
            f"- Source: {paper['url']}" + (f" (doi:{paper['doi']})" if paper["doi"] else ""),
            f"- Relevance: {review['reason'].strip()}",
            f"- Abstract: {paper['abstract']}",
            "",
        ]
    lines += ["## Discarded", ""]
    lines += [
        f"- [{review['paper_id']}] {by_id[review['paper_id']]['title']}: {review['reason'].strip()}"
        for review in reviews
        if review["decision"] == "discard"
    ]
    write_once(run_id, REVIEWS, json.dumps(reviews, indent=2, sort_keys=True))
    return write_once(run_id, REVIEWED, "\n".join(lines) + "\n")


def reviewed_papers_read(_arguments: dict[str, Any], run_id: str | None) -> str:
    return read_artifact(run_id, REVIEWED)


def literature_summary_write(arguments: dict[str, Any], run_id: str | None) -> dict[str, str]:
    paragraph = str(arguments["paragraph"]).strip()
    check_summary(paragraph, {review["paper_id"] for review in load_included(run_id)})
    return write_once(run_id, SUMMARY, paragraph + "\n")


def literature_summary_read(_arguments: dict[str, Any], run_id: str | None) -> str:
    return read_artifact(run_id, SUMMARY)


def future_directions_write(arguments: dict[str, Any], run_id: str | None) -> dict[str, str]:
    allowed = load_summary_refs(run_id)
    titles = {paper["paper_id"]: paper for paper in load_candidates(run_id)}
    lines = ["# Future research directions", ""]
    for index, item in enumerate(arguments["hypotheses"], start=1):
        evidence = list(dict.fromkeys(item["evidence"]))
        unknown = sorted(set(evidence) - allowed)
        if unknown:
            raise ArtifactError(f"hypothesis {index} cites papers absent from the summary: {unknown}")
        lines += [f"## H{index}. {item['hypothesis'].strip()}", "", item["rationale"].strip(), "", "Evidence:"]
        lines += [f"- [{paper_id}] {titles[paper_id]['title']} — {titles[paper_id]['url']}" for paper_id in evidence]
        lines.append("")
    return write_once(run_id, DIRECTIONS, "\n".join(lines))


TOOLS = {
    "exa_paper_search": exa_paper_search,
    "candidate_read": candidate_read,
    "reviewed_papers_write": reviewed_papers_write,
    "reviewed_papers_read": reviewed_papers_read,
    "literature_summary_write": literature_summary_write,
    "literature_summary_read": literature_summary_read,
    "future_directions_write": future_directions_write,
}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

ORCHESTRATOR_ID = "research-orchestrator"


@dataclass(frozen=True)
class Stage:
    agent_id: str
    artifact: str
    sources: tuple[str, ...]
    task: str
    validate: Callable[[str], object]

    def prompt_version(self) -> str:
        role_prompt = load_agent_registry().resolve_agent(self.agent_id).role_prompt
        return sha256(role_prompt + "\n" + self.task)[:12]


STAGES = (
    Stage(
        "web-researcher",
        CANDIDATES,
        (),
        "Find candidate research papers on: {topic}\n"
        "Call exa_paper_search once with a focused query and num_results between 10 and 15. "
        "If it reports an incomplete result, retry once with a broader query. Then reply with one line.",
        load_candidates,
    ),
    Stage(
        "paper-reviewer",
        REVIEWED,
        (CANDIDATES,),
        "Review the candidate papers for relevance to: {topic}\n"
        "Read them with candidate_read, then call reviewed_papers_write once with an include or discard decision "
        f"and a one-sentence reason for every candidate. Include at most {MAX_INCLUDED} papers. Then reply with one line.",
        load_included,
    ),
    Stage(
        "summary-writer",
        SUMMARY,
        (REVIEWED,),
        "Write the literature synthesis for: {topic}\n"
        "Read reviewed_papers_read, then call literature_summary_write with exactly one paragraph (no headings, "
        "no lists) that cites the papers inline as [paper_id]. Then reply with one line.",
        load_summary_refs,
    ),
    Stage(
        "hypothesis-generator",
        DIRECTIONS,
        (SUMMARY,),
        "Propose future research directions for: {topic}\n"
        "Read literature_summary_read, then call future_directions_write with 3 to 6 specific, testable hypotheses, "
        "each citing the [paper_id]s from the summary that motivate it. Then reply with one line.",
        check_directions,
    ),
)


class ResearchOrchestrator:
    def __init__(self, run_id: str, topic: str, *, credentials_dir: Path | None = None, stage_timeout: float = 900):
        run_dir(run_id)  # reject unsafe run ids before anything is launched
        self.run_id = run_id
        self.topic = topic
        self.credentials_dir = credentials_dir
        self.stage_timeout = stage_timeout
        self.trace_id = sha256(f"trace:{run_id}")[:32]
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
        self.recorder = InMemoryHarnessRecorder()
        self.nc: Any = None

    def traceparent(self, stage: str) -> str:
        return f"00-{self.trace_id}-{sha256(f'{self.run_id}:{stage}')[:16]}-01"

    async def publish(self, record: HarnessRecord) -> None:
        if self.nc is not None:
            await self.nc.publish(agent_lifecycle_subject(self.run_id, ORCHESTRATOR_ID), encode_signed_harness_record(record))

    async def run_agent(self, stage: Stage) -> int:
        env = agent_env(stage.agent_id, self.credentials_dir)
        env.pop("EXA_API_KEY", None)  # only the exa worker holds the Exa secret
        env["SWARMGUARD_TRACE_ID"] = self.trace_id
        task = stage.task.format(topic=self.topic)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "swarmguard.agent", stage.agent_id, "--run-id", self.run_id, "--task", task, env=env
        )
        try:
            return await asyncio.wait_for(process.wait(), timeout=self.stage_timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise ArtifactError(f"{stage.agent_id} timed out after {self.stage_timeout:.0f}s") from None

    async def run(self) -> dict[str, Any]:
        self.nc = await connect(ORCHESTRATOR_ID)
        report: dict[str, Any] = {"run_id": self.run_id, "trace_id": self.trace_id, "model": self.model, "stages": []}
        try:
            with start_run_span(self.run_id, agent_id=ORCHESTRATOR_ID, traceparent=self.traceparent("run")):
                await self.publish(self.recorder.run_started(run_id=self.run_id, agent_id=ORCHESTRATOR_ID))
                for index, stage in enumerate(STAGES):
                    entry: dict[str, Any] = {"agent_id": stage.agent_id, "artifact": stage.artifact}
                    report["stages"].append(entry)
                    try:
                        stage.validate(self.run_id)
                        entry["resumed"] = True  # artifact survived a restart: do not re-run the agent
                    except ArtifactError:
                        entry["resumed"] = False
                        entry["exit_code"] = await self.run_agent(stage)
                        if entry["exit_code"] != 0:
                            raise ArtifactError(f"{stage.agent_id} exited with {entry['exit_code']}")
                        stage.validate(self.run_id)
                    artifact = self.artifact_metadata(stage)
                    entry["sha256"] = artifact["digest"]
                    await self.publish(
                        self.recorder.record(
                            HarnessRecord(
                                kind="artifact",
                                run_id=self.run_id,
                                agent_id=ORCHESTRATOR_ID,
                                traceparent=artifact["traceparent"],
                                payload={"artifact": artifact},
                                record_id=f"artifact-{artifact['artifact_id']}-{artifact['digest'][:16]}",
                            )
                        )
                    )
                    recipient = STAGES[index + 1].agent_id if index + 1 < len(STAGES) else "operator"
                    await self.publish(
                        self.recorder.record(
                            HarnessRecord(
                                kind="handoff",
                                run_id=self.run_id,
                                agent_id=ORCHESTRATOR_ID,
                                agent_instance_id=ORCHESTRATOR_ID,
                                traceparent=artifact["traceparent"],
                                payload={
                                    "recipient": recipient,
                                    "message": f"validated {stage.artifact} ({artifact['digest'][:12]}) from {stage.agent_id}",
                                    "artifact_id": artifact["artifact_id"],
                                },
                                # Deterministic ids let JetStream/Postgres dedupe lineage events on resume.
                                record_id=f"handoff-{artifact['artifact_id']}-{artifact['digest'][:16]}",
                            )
                        )
                    )
        except ArtifactError as exc:
            report.update(status="failed", error=str(exc))
            await self.publish(
                self.recorder.run_ended(run_id=self.run_id, agent_id=ORCHESTRATOR_ID, status="failed", error=str(exc))
            )
        else:
            report["status"] = "completed"
            await self.publish(self.recorder.run_ended(run_id=self.run_id, agent_id=ORCHESTRATOR_ID, status="completed"))
        finally:
            await self.nc.drain()
        report["artifacts_dir"] = str(run_dir(self.run_id))
        return report

    def artifact_metadata(self, stage: Stage) -> dict[str, Any]:
        path = run_dir(self.run_id) / stage.artifact
        metadata = {
            "artifact_id": f"{self.run_id}:{stage.artifact}",
            "kind": stage.artifact.rsplit(".", 1)[0],
            "uri": str(path),
            "digest": sha256(path.read_bytes()),
            "producing_agent_id": stage.agent_id,
            "source_artifact_ids": [f"{self.run_id}:{source}" for source in stage.sources],
            "provider": "anthropic",
            "model": self.model,
            "prompt_version": stage.prompt_version(),
            "traceparent": self.traceparent(stage.agent_id),
            "validated": True,
        }
        if stage.artifact == CANDIDATES:
            metadata["raw_response_sha256"] = json.loads(path.read_text(encoding="utf-8"))["raw_response_sha256"]
        return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the four-agent deep-research workflow.")
    parser.add_argument("--topic", required=True)
    parser.add_argument("--run-id", default=f"research-{uuid4().hex[:8]}")
    parser.add_argument("--credentials-dir")
    parser.add_argument("--stage-timeout", type=float, default=900)
    args = parser.parse_args()
    orchestrator = ResearchOrchestrator(
        args.run_id,
        args.topic,
        credentials_dir=Path(args.credentials_dir) if args.credentials_dir else None,
        stage_timeout=args.stage_timeout,
    )
    report = asyncio.run(orchestrator.run())
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
