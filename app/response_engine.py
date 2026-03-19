# Kronaxis Panel Studio - Local Response Engine
# Copyright (c) 2026 Kronaxis Limited. All rights reserved.
# Licensed under BSL 1.1. See LICENSE file.
#
# Created by Jason Duke, Kronaxis Limited
# https://kronaxis.co.uk/dynamics
#
# Replaces the Animus HTTP proxy with a self-contained persona response
# generator that calls a local LLM server directly. This is the standalone
# version for the open-source Panel Studio distribution.

import json
import logging
import os
import threading
import uuid
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import psycopg2.extras
import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "")
MAX_WORKERS = int(os.environ.get("RESPONSE_CONCURRENCY", "4"))

# ---------------------------------------------------------------------------
# DYNAMICS-8 dimension descriptions (used in persona system prompts)
# ---------------------------------------------------------------------------

DYNAMICS_LABELS = {
    "D": ("Discipline", "self-regulation, planning, routine adherence"),
    "Y": ("Yielding", "agreeableness, compliance, conflict avoidance"),
    "N": ("Novelty", "openness to new experiences, curiosity"),
    "A": ("Acuity", "digital fluency, analytical depth"),
    "M": ("Mercuriality", "emotional volatility, anxiety proneness"),
    "I": ("Impulsivity", "decision speed, spontaneous action"),
    "C": ("Candour", "honesty, ethical concern, transparency"),
    "S": ("Sociability", "social engagement, group orientation"),
}


def _dynamics_description(dynamics: dict) -> str:
    """Build a human-readable DYNAMICS-8 personality description for an LLM prompt."""
    parts = []
    for dim in "DYNAMICS":
        score = dynamics.get(dim, 0.5)
        name, desc = DYNAMICS_LABELS.get(dim, (dim, ""))
        if score >= 0.7:
            level = "high"
        elif score <= 0.3:
            level = "low"
        else:
            level = "moderate"
        parts.append(f"- {dim} ({name}, {level} at {score:.2f}): {desc}")
    return "\n".join(parts)


def _label(score: float) -> str:
    if score >= 0.8:
        return "very high"
    if score >= 0.6:
        return "high"
    if score >= 0.4:
        return "moderate"
    if score >= 0.2:
        return "low"
    return "very low"


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

def build_system_prompt(persona: dict, memories: list = None) -> str:
    """Construct the system prompt for a persona response.

    The prompt instructs the LLM to respond as this specific person,
    shaped by their DYNAMICS-8 personality profile, demographics,
    life narrative, and accumulated memories.
    """
    name = persona.get("name", "Unknown")
    age = persona.get("age", "")
    gender = persona.get("gender", "")
    occupation = persona.get("occupation", "")
    location = persona.get("location", "")
    region = persona.get("region", "")
    dynamics = persona.get("dynamics", {})
    narrative = persona.get("life_narrative", "")

    dynamics_text = _dynamics_description(dynamics)

    memory_text = ""
    if memories:
        memory_lines = [f"- {m}" for m in memories[:20]]
        memory_text = "\nKEY MEMORIES AND EXPERIENCES:\n" + "\n".join(memory_lines)

    return f"""You are {name}, a {age}-year-old {gender} who works as {occupation} in {location}, {region}.

PERSONALITY PROFILE (DYNAMICS-8):
{dynamics_text}

LIFE NARRATIVE:
{narrative or 'No detailed narrative available.'}
{memory_text}

INSTRUCTIONS:
Respond to the following question or stimulus as {name} would. Your response must be shaped by your personality profile:
- Your Discipline score ({_label(dynamics.get('D', 0.5))}) affects how structured and considered your answer is.
- Your Yielding score ({_label(dynamics.get('Y', 0.5))}) affects whether you agree easily or push back.
- Your Novelty score ({_label(dynamics.get('N', 0.5))}) affects how open you are to new ideas.
- Your Acuity score ({_label(dynamics.get('A', 0.5))}) affects how analytically you approach the question.
- Your Mercuriality score ({_label(dynamics.get('M', 0.5))}) affects your emotional tone.
- Your Impulsivity score ({_label(dynamics.get('I', 0.5))}) affects how quickly you form an opinion.
- Your Candour score ({_label(dynamics.get('C', 0.5))}) affects how honest and direct you are.
- Your Sociability score ({_label(dynamics.get('S', 0.5))}) affects how engaged and verbose your response is.

Respond naturally as this person. Do not mention DYNAMICS-8 or personality scores. Do not break character. Keep your response between 50 and 200 words. Respond in first person."""


# ---------------------------------------------------------------------------
# LLM API client
# ---------------------------------------------------------------------------

def call_ollama(system_prompt: str, user_prompt: str, model: str = None) -> str:
    """Send a chat completion request to the local LLM server.

    Uses the OpenAI-compatible endpoint for broad model support.
    """
    model = model or OLLAMA_MODEL
    url = OLLAMA_URL.rstrip("/") + "/api/chat"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {
            "temperature": 0.8,
            "top_p": 0.9,
        },
    }

    # Some models wrap output in thinking tags; limit output length.
    payload["options"]["num_predict"] = 512

    try:
        resp = requests.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        content = data.get("message", {}).get("content", "")
        # Strip any <think> tags that some models produce.
        if "<think>" in content:
            import re
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        return content
    except requests.RequestException as e:
        log.error("LLM request failed: %s", e)
        return f"[Response generation failed: {e}]"


# ---------------------------------------------------------------------------
# Response Engine
# ---------------------------------------------------------------------------

class ResponseEngine:
    """Self-contained persona response generator using a local LLM server.

    Replaces the Animus HTTP proxy for the standalone Panel Studio.
    """

    def __init__(self, db_pool):
        self.pool = db_pool

    def _get_conn(self):
        return self.pool.getconn()

    def _put_conn(self, conn):
        self.pool.putconn(conn)

    def _get_persona(self, conn, persona_id: str) -> dict:
        """Fetch a persona by ID."""
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, name, age, gender, ethnicity, occupation, location, region, "
                "dynamics, life_narrative, mode, status FROM soul_personas WHERE id = %s",
                (persona_id,)
            )
            row = cur.fetchone()
            if row:
                row = dict(row)
                if isinstance(row.get("dynamics"), str):
                    row["dynamics"] = json.loads(row["dynamics"])
            return row

    def _get_memories(self, conn, persona_id: str, limit: int = 20) -> list:
        """Fetch recent memories for a persona."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT content FROM soul_memory WHERE persona_id = %s "
                "ORDER BY importance DESC, created_at DESC LIMIT %s",
                (persona_id, limit)
            )
            return [row[0] for row in cur.fetchall()]

    def _get_panel_personas(self, conn, panel_id: str) -> list:
        """Fetch persona IDs for a panel."""
        with conn.cursor() as cur:
            cur.execute("SELECT persona_ids FROM soul_panels WHERE id = %s", (panel_id,))
            row = cur.fetchone()
            if row and row[0]:
                return [str(pid) for pid in row[0]]
            return []

    def generate_response(self, persona_id: str, stimulus: str,
                          conversation_history: list = None) -> dict:
        """Generate a single persona response to a stimulus."""
        conn = self._get_conn()
        try:
            persona = self._get_persona(conn, persona_id)
            if not persona:
                return {"error": f"Persona {persona_id} not found"}

            memories = self._get_memories(conn, persona_id)
            system_prompt = build_system_prompt(persona, memories)

            # Build the user message with conversation history if provided.
            if conversation_history:
                history_text = "\n\nPREVIOUS EXCHANGE:\n"
                for turn in conversation_history[-3:]:
                    history_text += f"Question: {turn.get('stimulus', '')}\n"
                    history_text += f"Your previous answer: {turn.get('response', '')}\n"
                user_prompt = history_text + f"\nNEW QUESTION: {stimulus}"
            else:
                user_prompt = stimulus

            start = time.time()
            response_text = call_ollama(system_prompt, user_prompt)
            latency_ms = int((time.time() - start) * 1000)

            return {
                "persona_id": str(persona["id"]),
                "persona_name": persona["name"],
                "response": response_text,
                "dynamics": persona.get("dynamics", {}),
                "age": persona.get("age"),
                "gender": persona.get("gender"),
                "location": persona.get("location"),
                "region": persona.get("region"),
                "occupation": persona.get("occupation"),
                "latency_ms": latency_ms,
            }
        finally:
            self._put_conn(conn)

    def stimulate_panel(self, panel_id: str, stimulus: str,
                        stimulus_type: str = "standard",
                        sample_size: int = None) -> str:
        """Run a stimulus across all (or sampled) personas in a panel.

        Creates a soul_panel_runs row and generates responses in parallel.
        Returns a run_id for polling.
        """
        conn = self._get_conn()
        try:
            persona_ids = self._get_panel_personas(conn, panel_id)
            if not persona_ids:
                raise ValueError(f"Panel {panel_id} has no personas")

            # Sample if requested.
            if sample_size and sample_size < len(persona_ids):
                import random
                persona_ids = random.sample(persona_ids, sample_size)

            # Create the run record.
            run_id = str(uuid.uuid4())
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO soul_panel_runs (id, panel_id, stimulus, stimulus_type, status) "
                    "VALUES (%s, %s, %s, %s, 'running')",
                    (run_id, panel_id, stimulus, stimulus_type)
                )
                conn.commit()

            # Launch background thread to process responses.
            thread = threading.Thread(
                target=self._process_panel_responses,
                args=(run_id, panel_id, persona_ids, stimulus),
                daemon=True,
            )
            thread.start()

            return run_id
        finally:
            self._put_conn(conn)

    def _process_panel_responses(self, run_id: str, panel_id: str,
                                  persona_ids: list, stimulus: str):
        """Background worker: generate responses for all personas in parallel."""
        responses = []
        errors = 0

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(self.generate_response, pid, stimulus): pid
                for pid in persona_ids
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if "error" not in result:
                        responses.append(result)
                    else:
                        errors += 1
                        log.warning("Response error for %s: %s", futures[future], result["error"])
                except Exception as e:
                    errors += 1
                    log.error("Response generation failed: %s", e)

        # Aggregate responses.
        aggregated = self._aggregate(responses)

        # Update the run record.
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE soul_panel_runs SET raw_responses = %s, aggregated_responses = %s, "
                    "status = 'completed', completed_at = NOW() WHERE id = %s",
                    (json.dumps(responses), json.dumps(aggregated), run_id)
                )
                conn.commit()
            log.info("Panel run %s completed: %d responses, %d errors", run_id, len(responses), errors)
        finally:
            self._put_conn(conn)

    def _aggregate(self, responses: list) -> dict:
        """Aggregate individual persona responses into demographic summaries."""
        if not responses:
            return {"total": 0, "by_age_group": {}, "by_gender": {}, "by_region": {}}

        total = len(responses)

        # Group by age band.
        age_groups = {}
        for r in responses:
            age = r.get("age", 0)
            if age < 25:
                band = "18-24"
            elif age < 35:
                band = "25-34"
            elif age < 45:
                band = "35-44"
            elif age < 55:
                band = "45-54"
            elif age < 65:
                band = "55-64"
            else:
                band = "65+"
            age_groups.setdefault(band, []).append(r["response"])

        # Group by gender.
        gender_groups = {}
        for r in responses:
            g = r.get("gender", "unknown")
            gender_groups.setdefault(g, []).append(r["response"])

        # Group by region.
        region_groups = {}
        for r in responses:
            reg = r.get("region", "unknown")
            region_groups.setdefault(reg, []).append(r["response"])

        return {
            "total": total,
            "by_age_group": {k: {"count": len(v), "sample": v[0] if v else ""} for k, v in age_groups.items()},
            "by_gender": {k: {"count": len(v), "sample": v[0] if v else ""} for k, v in gender_groups.items()},
            "by_region": {k: {"count": len(v), "sample": v[0] if v else ""} for k, v in region_groups.items()},
        }

    def get_run_status(self, run_id: str) -> dict:
        """Check the status of a panel run."""
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, panel_id, stimulus, status, raw_responses, aggregated_responses, "
                    "created_at, completed_at FROM soul_panel_runs WHERE id = %s",
                    (run_id,)
                )
                row = cur.fetchone()
                if not row:
                    return {"error": "Run not found"}
                result = dict(row)
                # Count responses if available.
                raw = result.get("raw_responses")
                if raw and isinstance(raw, list):
                    result["response_count"] = len(raw)
                elif raw and isinstance(raw, str):
                    try:
                        result["response_count"] = len(json.loads(raw))
                    except (json.JSONDecodeError, TypeError):
                        result["response_count"] = 0
                else:
                    result["response_count"] = 0
                # Serialise datetime fields.
                for k in ("created_at", "completed_at"):
                    if result.get(k):
                        result[k] = result[k].isoformat()
                return result
        finally:
            self._put_conn(conn)

    def get_segments(self, panel_id: str) -> dict:
        """Get demographic breakdown of a panel's personas."""
        conn = self._get_conn()
        try:
            persona_ids = self._get_panel_personas(conn, panel_id)
            if not persona_ids:
                return {"total": 0, "by_age_group": {}, "by_gender": {}, "by_region": {}}

            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT age, gender, region, occupation FROM soul_personas WHERE id = ANY(%s)",
                    (persona_ids,)
                )
                personas = cur.fetchall()

            age_groups = {}
            gender_groups = {}
            region_groups = {}
            occupation_groups = {}

            for p in personas:
                age = p.get("age", 0)
                if age < 25:
                    band = "18-24"
                elif age < 35:
                    band = "25-34"
                elif age < 45:
                    band = "35-44"
                elif age < 55:
                    band = "45-54"
                elif age < 65:
                    band = "55-64"
                else:
                    band = "65+"
                age_groups[band] = age_groups.get(band, 0) + 1
                gender_groups[p.get("gender", "unknown")] = gender_groups.get(p.get("gender", "unknown"), 0) + 1
                region_groups[p.get("region", "unknown")] = region_groups.get(p.get("region", "unknown"), 0) + 1
                occ = p.get("occupation", "unknown")
                occupation_groups[occ] = occupation_groups.get(occ, 0) + 1

            return {
                "total": len(personas),
                "by_age_group": age_groups,
                "by_gender": gender_groups,
                "by_region": region_groups,
                "by_occupation": dict(sorted(occupation_groups.items(), key=lambda x: -x[1])[:20]),
            }
        finally:
            self._put_conn(conn)

    def generate_focus_group(self, run_id: str, turn_number: int = None) -> dict:
        """Synthesise a focus group discussion from individual panel responses.

        Takes the raw responses from a completed run and asks the LLM to
        generate a naturalistic group discussion.
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT raw_responses, stimulus FROM soul_panel_runs WHERE id = %s AND status = 'completed'",
                    (run_id,)
                )
                row = cur.fetchone()
                if not row:
                    return {"error": "Run not found or not completed"}

            raw = row["raw_responses"]
            if isinstance(raw, str):
                raw = json.loads(raw)
            stimulus = row["stimulus"]

            if not raw or len(raw) < 2:
                return {"error": "Not enough responses for a focus group"}

            # Select up to 8 diverse participants.
            import random
            participants = random.sample(raw, min(8, len(raw)))

            participant_text = "\n".join([
                f"- {p['persona_name']} ({p.get('age', '?')}, {p.get('gender', '?')}, {p.get('occupation', '?')}): \"{p['response']}\""
                for p in participants
            ])

            system = (
                "You are a skilled focus group moderator. Given a stimulus question and individual "
                "participant responses, generate a naturalistic focus group discussion where participants "
                "build on, challenge, and respond to each other's views. Keep each speaking turn to "
                "1-3 sentences. Include 4-6 exchanges. Use participant names."
            )

            user = (
                f"STIMULUS: {stimulus}\n\n"
                f"INDIVIDUAL RESPONSES:\n{participant_text}\n\n"
                "Generate the focus group discussion."
            )

            discussion = call_ollama(system, user)
            return {
                "focus_group": discussion,
                "participants": [{"name": p["persona_name"], "age": p.get("age")} for p in participants],
                "stimulus": stimulus,
            }
        finally:
            self._put_conn(conn)

    def create_panel(self, name: str, description: str, persona_ids: list,
                     spec: dict = None, owner_id: str = None) -> dict:
        """Create a new panel directly in the database."""
        conn = self._get_conn()
        try:
            panel_id = str(uuid.uuid4())
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO soul_panels (id, name, description, persona_ids, spec, owner_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (panel_id, name, description, persona_ids,
                     json.dumps(spec) if spec else None, owner_id)
                )
                conn.commit()
            return {"id": panel_id, "name": name, "persona_count": len(persona_ids)}
        finally:
            self._put_conn(conn)

    def health(self) -> dict:
        """Check LLM server connectivity and model availability."""
        try:
            resp = requests.get(OLLAMA_URL.rstrip("/") + "/api/tags", timeout=5)
            resp.raise_for_status()
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            model_available = any(OLLAMA_MODEL in m for m in models)
            return {
                "status": "ok" if model_available else "model_missing",
                "ollama_url": OLLAMA_URL,
                "model": OLLAMA_MODEL,
                "model_available": model_available,
                "available_models": models,
            }
        except requests.RequestException as e:
            return {
                "status": "unavailable",
                "ollama_url": OLLAMA_URL,
                "error": str(e),
            }
