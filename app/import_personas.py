#!/usr/bin/env python3
"""
Persona Import — Load JSONL personas into soul_personas + soul_memory.

Supports two formats:
  1. Flat (seed) format: {name, age, gender, dynamics, location, region, ...}
  2. Nested (Imprint) format: {persona_id, identity: {...}, dynamics_8: {...}, ...}

Usage:
    python3 import_personas.py --jsonl <path> --db <connection_string> [--panel-name <name>] [--limit <n>]

Example:
    python3 import_personas.py \
        --jsonl /seed/personas.jsonl \
        --db postgresql://titan:${TFS_DB_PASSWORD}@kps-db:5432/tfs \
        --panel-name "UK National Panel"
"""

import argparse
import json
import sys
import uuid
import logging
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

psycopg2.extras.register_uuid()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [import] %(message)s")
log = logging.getLogger(__name__)

# UUID v5 namespace for deterministic persona UUIDs.
KRONAXIS_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def persona_uuid(identifier: str) -> uuid.UUID:
    """Generate a deterministic UUID from a persona identifier."""
    return uuid.uuid5(KRONAXIS_NS, identifier)


def _detect_format(persona: dict) -> str:
    """Detect whether the persona record is flat or nested."""
    if "identity" in persona and isinstance(persona["identity"], dict):
        return "nested"
    return "flat"


def _extract_flat(persona: dict, line_num: int) -> dict:
    """Extract fields from flat format (seed data)."""
    raw_id = persona.get("persona_id", persona.get("name", f"seed-{line_num}"))
    dynamics = persona.get("dynamics", {})
    if isinstance(dynamics, str):
        dynamics = json.loads(dynamics)
    return {
        "id": persona_uuid(raw_id),
        "name": persona.get("name", "Unknown"),
        "age": persona.get("age"),
        "gender": persona.get("gender"),
        "ethnicity": persona.get("ethnicity"),
        "occupation": persona.get("occupation"),
        "occupation_sector": persona.get("occupation_sector"),
        "education_level": persona.get("education_level"),
        "annual_income": persona.get("annual_income"),
        "location": persona.get("location"),
        "region": persona.get("region"),
        "dynamics": dynamics,
        "life_narrative": persona.get("life_narrative"),
        "biography": persona.get("biography"),
        "mode": persona.get("mode", "synthetic"),
        "bio_summary": f"{persona.get('name', 'Unknown')} is {persona.get('age', '?')} years old, "
                       f"works as {persona.get('occupation', 'unknown')} in {persona.get('location', 'unknown')}, "
                       f"{persona.get('region', '')}.",
    }


def _extract_nested(persona: dict, line_num: int) -> dict:
    """Extract fields from nested Imprint format."""
    raw_id = persona.get("persona_id", f"UNKNOWN-{line_num}")
    identity = persona.get("identity", {})
    dynamics = persona.get("dynamics_8", {})

    first = identity.get("first_name", "")
    surname = identity.get("surname", "")
    name = f"{first} {surname}".strip()

    region = identity.get("region", "")
    town = identity.get("town", "")
    location = f"{region}, {town}" if town and region else (region or town)

    age = identity.get("age")
    occupation = identity.get("occupation", "")

    parts = [f"{name} is {age} years old"]
    if occupation:
        parts.append(f"works as a {occupation}")
    if location:
        parts.append(f"lives in {location}")
    education = identity.get("education_level", "")
    if education:
        parts.append(f"educated to {education} level")
    income = identity.get("annual_income")
    if income:
        try:
            parts.append(f"earns approximately \u00a3{int(income):,} per year")
        except (ValueError, TypeError):
            pass

    return {
        "id": persona_uuid(raw_id),
        "name": name,
        "age": age,
        "gender": identity.get("gender"),
        "ethnicity": identity.get("ethnicity"),
        "occupation": occupation,
        "occupation_sector": identity.get("occupation_sector"),
        "education_level": identity.get("education_level"),
        "annual_income": identity.get("annual_income"),
        "location": location,
        "region": region,
        "dynamics": dynamics,
        "life_narrative": json.dumps(persona),
        "biography": persona.get("biography"),
        "mode": "synthetic",
        "bio_summary": ". ".join(parts) + ".",
        "episodic": persona.get("memory", {}).get("episodic", []),
    }


def import_personas(jsonl_path, db_dsn, panel_name, limit):
    conn = psycopg2.connect(db_dsn)
    conn.autocommit = False
    cur = conn.cursor()

    persona_ids = []
    imported = 0
    skipped = 0
    memories_created = 0

    log.info("Reading %s ...", jsonl_path)

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            if limit and imported >= limit:
                break

            line = line.strip()
            if not line:
                continue

            try:
                persona = json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("Line %d: invalid JSON, skipping: %s", line_num, e)
                skipped += 1
                continue

            fmt = _detect_format(persona)
            if fmt == "nested":
                fields = _extract_nested(persona, line_num)
            else:
                fields = _extract_flat(persona, line_num)

            pid = fields["id"]

            cur.execute("""
                INSERT INTO soul_personas
                    (id, name, age, gender, ethnicity, occupation, occupation_sector,
                     education_level, annual_income, location, region, dynamics,
                     life_narrative, biography, mode, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
                ON CONFLICT (id) DO UPDATE SET
                    dynamics = EXCLUDED.dynamics,
                    life_narrative = EXCLUDED.life_narrative,
                    name = EXCLUDED.name,
                    age = EXCLUDED.age,
                    occupation = EXCLUDED.occupation,
                    location = EXCLUDED.location,
                    region = EXCLUDED.region,
                    updated_at = NOW()
            """, (
                pid,
                fields["name"],
                fields["age"],
                fields["gender"],
                fields["ethnicity"],
                fields["occupation"],
                fields["occupation_sector"],
                fields["education_level"],
                fields["annual_income"],
                fields["location"],
                fields["region"],
                json.dumps(fields["dynamics"]),
                fields["life_narrative"],
                json.dumps(fields["biography"]) if fields.get("biography") else None,
                fields["mode"],
            ))

            persona_ids.append(pid)
            imported += 1

            # Seed biographical memory.
            bio = fields.get("bio_summary", "")
            if bio:
                mem_id = uuid.uuid5(KRONAXIS_NS, f"{pid}-bio")
                cur.execute("""
                    INSERT INTO soul_memory
                        (id, persona_id, content, entry_type, source, importance)
                    VALUES (%s, %s, %s, 'lived', 'import', 0.7)
                    ON CONFLICT (id) DO NOTHING
                """, (mem_id, pid, bio))
                memories_created += 1

            # Seed episodic memories (nested format only).
            for i, mem in enumerate(fields.get("episodic", [])[:4]):
                content = mem if isinstance(mem, str) else mem.get("content", "")
                if not content:
                    continue
                ep_id = uuid.uuid5(KRONAXIS_NS, f"{pid}-ep-{i}")
                cur.execute("""
                    INSERT INTO soul_memory
                        (id, persona_id, content, entry_type, source, importance)
                    VALUES (%s, %s, %s, 'lived', 'import', 0.5)
                    ON CONFLICT (id) DO NOTHING
                """, (ep_id, pid, content))
                memories_created += 1

            if imported % 500 == 0:
                conn.commit()
                log.info("Progress: %d imported, %d skipped", imported, skipped)

    conn.commit()
    log.info("Personas imported: %d, skipped: %d, memories: %d", imported, skipped, memories_created)

    # Create default panel.
    if persona_ids:
        panel_id = uuid.uuid5(KRONAXIS_NS, panel_name)
        cur.execute("""
            INSERT INTO soul_panels (id, name, description, persona_ids, spec, status)
            VALUES (%s, %s, %s, %s, %s, 'active')
            ON CONFLICT (id) DO UPDATE SET
                persona_ids = EXCLUDED.persona_ids,
                description = EXCLUDED.description,
                spec = EXCLUDED.spec
        """, (
            panel_id,
            panel_name,
            f"Seed panel with {len(persona_ids)} synthetic personas. "
            f"DYNAMICS-8 personality profiles, census-weighted UK demographics.",
            persona_ids,
            json.dumps({"type": "import", "source": "jsonl", "country": "UK"}),
        ))
        conn.commit()
        log.info("Panel '%s' created/updated with %d personas (id: %s)",
                 panel_name, len(persona_ids), panel_id)

    cur.close()
    conn.close()

    log.info("Import complete.")
    return imported, skipped, memories_created


def main():
    parser = argparse.ArgumentParser(description="Import personas into Panel Studio")
    parser.add_argument("--jsonl", required=True, help="Path to JSONL persona file")
    parser.add_argument("--db", required=True, help="PostgreSQL connection string")
    parser.add_argument("--panel-name", default="UK National Panel", help="Default panel name")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of personas (0 = all)")
    args = parser.parse_args()

    imported, skipped, memories = import_personas(args.jsonl, args.db, args.panel_name, args.limit)
    print(f"\nSummary: {imported} imported, {skipped} skipped, {memories} memories seeded")


if __name__ == "__main__":
    main()
