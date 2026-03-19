"""
Export Engine — JSONL, Parquet, CSV export for conversation data.

Exports structured causal reasoning data from panel conversations.
Each record contains persona demographics, DYNAMICS-8, stimulus, response,
reasoning trace, and sentiment.
"""

import io
import json
import csv
import tempfile
import logging
from datetime import datetime

import psycopg2.extras

log = logging.getLogger(__name__)


def _fetch_conversation_data(conversation_id, conn):
    """Fetch all turns with raw responses and persona data for a conversation."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Fetch conversation metadata.
    cur.execute("""
        SELECT pc.id, pc.panel_id, pc.title, pc.turn_count,
               sp.name AS panel_name
        FROM panel_conversations pc
        JOIN soul_panels sp ON sp.id = pc.panel_id
        WHERE pc.id = %s
    """, (conversation_id,))
    conv = cur.fetchone()
    if not conv:
        cur.close()
        return None, None, None

    # Fetch all turns.
    cur.execute("""
        SELECT id, turn_number, stimulus, stimulus_type, run_id,
               response_count, aggregated, created_at, completed_at
        FROM panel_conversation_turns
        WHERE conversation_id = %s
        ORDER BY turn_number
    """, (conversation_id,))
    turns = cur.fetchall()

    # Fetch raw responses for each turn via soul_panel_runs.
    all_responses = []
    for turn in turns:
        if not turn["run_id"]:
            continue
        cur.execute("""
            SELECT spr.raw_responses
            FROM soul_panel_runs spr
            WHERE spr.id = %s
        """, (turn["run_id"],))
        run = cur.fetchone()
        if run and run["raw_responses"]:
            responses = run["raw_responses"]
            if isinstance(responses, str):
                try:
                    responses = json.loads(responses)
                except json.JSONDecodeError:
                    responses = []
            for resp in responses:
                resp["turn_number"] = turn["turn_number"]
                resp["stimulus"] = turn["stimulus"]
            all_responses.extend(responses)

    # Fetch persona demographics for enrichment.
    persona_ids = list({r.get("persona_id") for r in all_responses if r.get("persona_id")})
    personas = {}
    if persona_ids:
        cur.execute("""
            SELECT id, name, age, occupation, location, dynamics
            FROM soul_personas WHERE id = ANY(%s::uuid[])
        """, (persona_ids,))
        for p in cur.fetchall():
            personas[str(p["id"])] = p

    cur.close()
    return conv, turns, _enrich_responses(all_responses, personas, conversation_id)


def _enrich_responses(responses, personas, conversation_id):
    """Attach persona demographics and DYNAMICS-8 to each response."""
    enriched = []
    for resp in responses:
        pid = str(resp.get("persona_id", ""))
        persona = personas.get(pid, {})
        dynamics = persona.get("dynamics", {})
        if isinstance(dynamics, str):
            try:
                dynamics = json.loads(dynamics)
            except (json.JSONDecodeError, TypeError):
                dynamics = {}

        enriched.append({
            "persona_id": pid,
            "conversation_id": str(conversation_id),
            "turn": resp.get("turn_number", 0),
            "stimulus": resp.get("stimulus", ""),
            "response": resp.get("reaction", "") or resp.get("response", ""),
            "intent": resp.get("intent", ""),
            "sentiment": resp.get("sentiment", ""),
            "confidence": resp.get("confidence", 0),
            "influence_score": resp.get("influence_score", 0),
            "dynamics_8": dynamics,
            "demographics": {
                "name": persona.get("name", ""),
                "age": persona.get("age"),
                "occupation": persona.get("occupation", ""),
                "location": persona.get("location", ""),
            },
        })
    return enriched


def export_conversation_jsonl(conversation_id, conn):
    """Export conversation as JSONL. Returns (data_string, filename)."""
    conv, turns, records = _fetch_conversation_data(conversation_id, conn)
    if not conv:
        return "", "empty.jsonl"

    lines = []
    for record in records:
        lines.append(json.dumps(record, default=str))

    filename = f"panel_conversation_{conversation_id}.jsonl"
    return "\n".join(lines) + "\n", filename


def export_conversation_parquet(conversation_id, conn):
    """Export conversation as Parquet. Returns (filepath, filename)."""
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    conv, turns, records = _fetch_conversation_data(conversation_id, conn)
    if not conv:
        records = []

    # Flatten for columnar format.
    flat = []
    for r in records:
        row = {
            "persona_id": r["persona_id"],
            "conversation_id": r["conversation_id"],
            "turn": r["turn"],
            "stimulus": r["stimulus"],
            "response": r["response"],
            "intent": r.get("intent", ""),
            "sentiment": r["sentiment"],
            "name": r["demographics"].get("name", ""),
            "age": r["demographics"].get("age"),
            "occupation": r["demographics"].get("occupation", ""),
            "location": r["demographics"].get("location", ""),
        }
        # Flatten DYNAMICS-8 dimensions.
        for dim in ["D", "Y", "N", "A", "M", "I", "C", "S"]:
            row[f"dynamics_{dim}"] = r["dynamics_8"].get(dim, 0.0)
        flat.append(row)

    df = pd.DataFrame(flat)
    filename = f"panel_conversation_{conversation_id}.parquet"
    tmp = tempfile.NamedTemporaryFile(suffix=".parquet", delete=False)
    tmp.close()
    df.to_parquet(tmp.name, engine="pyarrow", index=False)
    return tmp.name, filename


def export_conversation_csv(conversation_id, conn):
    """Export aggregated conversation as CSV. Returns (data_string, filename)."""
    conv, turns, records = _fetch_conversation_data(conversation_id, conn)
    if not conv:
        return "", "empty.csv"

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "turn", "stimulus", "response_count",
        "positive_pct", "negative_pct", "neutral_pct", "mixed_pct",
        "top_themes",
    ])

    for turn in turns:
        agg = turn.get("aggregated") or {}
        if isinstance(agg, str):
            try:
                agg = json.loads(agg)
            except (json.JSONDecodeError, TypeError):
                agg = {}

        sentiment = agg.get("sentiment", {})
        themes = agg.get("themes", [])
        writer.writerow([
            turn["turn_number"],
            turn["stimulus"],
            turn["response_count"],
            sentiment.get("positive", 0),
            sentiment.get("negative", 0),
            sentiment.get("neutral", 0),
            sentiment.get("mixed", 0),
            "; ".join(themes[:5]) if themes else "",
        ])

    filename = f"panel_conversation_{conversation_id}_summary.csv"
    return output.getvalue(), filename


def _fetch_focus_group_transcripts(conversation_id, conn):
    """Fetch all focus group transcripts for a conversation's turns."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT pct.turn_number, pct.stimulus,
               fg.transcript, fg.speaker_count, fg.word_count,
               fg.generated_at, fg.model_used
        FROM panel_conversation_turns pct
        JOIN panel_focus_group_transcripts fg ON fg.run_id = pct.run_id
        WHERE pct.conversation_id = %s
        ORDER BY pct.turn_number
    """, (conversation_id,))
    transcripts = cur.fetchall()
    cur.close()
    return transcripts


def export_focus_group_jsonl(conversation_id, conn):
    """Export focus group transcripts as JSONL. Returns (data_string, filename)."""
    transcripts = _fetch_focus_group_transcripts(conversation_id, conn)
    if not transcripts:
        return "", "empty_focus_group.jsonl"

    lines = []
    for t in transcripts:
        lines.append(json.dumps({
            "conversation_id": str(conversation_id),
            "turn": t["turn_number"],
            "stimulus": t["stimulus"],
            "transcript": t["transcript"],
            "speaker_count": t["speaker_count"],
            "word_count": t["word_count"],
            "generated_at": str(t["generated_at"]),
            "model_used": t["model_used"],
        }, default=str))

    filename = f"panel_focus_group_{conversation_id}.jsonl"
    return "\n".join(lines) + "\n", filename


def export_conversation_jsonl_full(conversation_id, conn):
    """Export conversation JSONL with focus group transcripts appended."""
    # Get the standard response records.
    data, filename = export_conversation_jsonl(conversation_id, conn)
    if not data:
        return "", "empty.jsonl"

    # Append focus group transcripts as separate records.
    transcripts = _fetch_focus_group_transcripts(conversation_id, conn)
    fg_lines = []
    for t in transcripts:
        fg_lines.append(json.dumps({
            "record_type": "focus_group_transcript",
            "conversation_id": str(conversation_id),
            "turn": t["turn_number"],
            "stimulus": t["stimulus"],
            "transcript": t["transcript"],
            "speaker_count": t["speaker_count"],
            "word_count": t["word_count"],
            "generated_at": str(t["generated_at"]),
        }, default=str))

    if fg_lines:
        data += "\n".join(fg_lines) + "\n"

    filename = f"panel_conversation_{conversation_id}_full.jsonl"
    return data, filename


def export_bulk_jsonl(conversation_ids, conn):
    """Export multiple conversations as a single JSONL file."""
    all_lines = []
    for cid in conversation_ids:
        conv, turns, records = _fetch_conversation_data(cid, conn)
        if records:
            for record in records:
                all_lines.append(json.dumps(record, default=str))

    filename = f"panel_training_data_{len(conversation_ids)}convs.jsonl"
    return "\n".join(all_lines) + "\n", filename
