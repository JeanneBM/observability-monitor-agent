"""Optional triage layer. Turns deterministic findings into a readable summary.

Design constraints:
  * It only *describes*. It has no tools and cannot act on other agents.
  * It sees findings (rule, message, ids), not raw prompts or tool arguments.
  * Detection never depends on it. If it fails, the findings are still reported.
"""
from __future__ import annotations

import json
import os
from typing import Protocol

from .schema import Finding


class Summarizer(Protocol):
    def summarize(self, findings: list[Finding]) -> str: ...


class OfflineSummarizer:
    """Deterministic fallback, no network, no model."""

    def summarize(self, findings: list[Finding]) -> str:
        if not findings:
            return "No anomalies detected."
        by_agent: dict[str, list[Finding]] = {}
        for f in findings:
            by_agent.setdefault(f.agent, []).append(f)
        out = [f"{len(findings)} finding(s) across {len(by_agent)} agent(s):"]
        for agent, fs in by_agent.items():
            out.append(f"\n## {agent}")
            for f in fs:
                where = f" (run {f.run_id})" if f.run_id else ""
                out.append(f"- [{f.severity.upper()}] {f.rule}{where}: {f.message}")
        return "\n".join(out)


SYSTEM_PROMPT = (
    "You are a read-only triage assistant for an AI-agent monitoring system. "
    "You receive deterministic findings as JSON. Group related findings, point out "
    "likely common causes, and rank what a human should look at first. Do not invent "
    "facts that are not in the findings. Do not propose automated actions."
)


class AzureOpenAISummarizer:
    """Azure OpenAI triage. Requires `pip install .[llm]`.

    Env: AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT. Uses Entra ID
    (DefaultAzureCredential / Workload Identity), so no API key is needed.
    Not exercised by the test-suite (needs a live endpoint).
    """

    def __init__(self, endpoint: str | None = None, deployment: str | None = None):
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import AzureOpenAI

        self.deployment = deployment or os.environ["AZURE_OPENAI_DEPLOYMENT"]
        token = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
        )
        self.client = AzureOpenAI(
            azure_endpoint=endpoint or os.environ["AZURE_OPENAI_ENDPOINT"],
            azure_ad_token_provider=token,
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )

    def summarize(self, findings: list[Finding]) -> str:
        payload = json.dumps([f.to_dict() for f in findings], indent=2)
        resp = self.client.chat.completions.create(
            model=self.deployment,
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
        )
        return resp.choices[0].message.content or ""


def triage(findings: list[Finding], summarizer: Summarizer | None = None) -> str:
    """Summarize findings; fall back to the offline summary if the LLM fails."""
    if not findings:
        return "No anomalies detected."
    s = summarizer or OfflineSummarizer()
    try:
        return s.summarize(findings)
    except Exception as exc:  # noqa: BLE001 - detection must never depend on the LLM
        return OfflineSummarizer().summarize(findings) + f"\n\n(LLM triage unavailable: {exc})"
