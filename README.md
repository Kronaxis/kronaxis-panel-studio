<p align="center">
  <img src="assets/kronaxis-icon.svg" width="64" height="64" alt="Kronaxis">
</p>

<h1 align="center">Kronaxis Panel Studio</h1>

<p align="center">
  <strong>1,000 synthetic consumers in 30 seconds. Self-hosted. No human panelists, no recruitment, no incentive payments.</strong>
</p>

<p align="center">
  <a href="LICENSE">BSL 1.1</a> &middot;
  <a href="https://kronaxis.co.uk">Website</a> &middot;
  <a href="COMMERCIAL_LICENCE.md">Commercial Licence</a> &middot;
  <a href="lib/dynamics/DYNAMICS-8.md">DYNAMICS-8 Spec</a>
</p>

---

## What is Panel Studio?

Traditional consumer research costs £10,000–£50,000 per study, takes weeks to recruit, and pays panelists who lie or rush. Panel Studio replaces the panel with **thousands of simulated consumers**, each with a unique [DYNAMICS-8](https://github.com/Kronaxis/dynamics-8) personality, a coherent life history, and census-weighted demographics. Submit a product concept, an ad, a policy proposal — every persona responds in character, producing demographically segmented sentiment data in minutes instead of weeks.

It runs entirely on your hardware via a local LLM server. Your stimuli, your sentiment data, and your derived insights stay on your machine.

**500 pre-loaded UK personas are included.** Start running stimuli immediately after `docker-compose up`.

## How it compares to traditional and AI consumer research

| | Panel Studio | Synthetic Users / SyntheticUsers.com | Resemble.ai (synthetic data) | Traditional panel (Quester / Kantar / Nielsen) |
|---|---|---|---|---|
| **Cost per study** | Self-hosted: just GPU electricity | £10–50/persona; £500+/study | Subscription (varies) | £10,000–£50,000+ |
| **Time to first response** | ~30 seconds (local LLM) | minutes (cloud) | minutes (cloud) | 1–6 weeks (recruitment + fielding) |
| **Personality framework** | DYNAMICS-8 (Big Five + HEXACO + 2 digital-age dimensions) | proprietary | proprietary | demographic only |
| **Demographic weighting** | Census-weighted via country builders (20 countries) | US-centric | varies | targetable but expensive |
| **Data residency** | Fully local; nothing leaves your machine | Cloud only | Cloud only | varies |
| **Reproducibility** | Same persona ID + same stimulus = same response | not deterministic | not deterministic | impossible (human variability) |
| **Public falsifiable validation** | Yes — see [KPM-1 election predictions](https://github.com/Kronaxis/kpm1-election-projections) | No | No | (not the model's claim) |
| **Source available** | ✓ (BSL 1.1) | ✗ | ✗ | ✗ |
| **Best for** | Fast iteration, sensitive stimuli, longitudinal studies | Hosted convenience | Adjacent use case (training data) | Regulatory / publishable studies that demand human panels |

If your study needs to be defensible in a regulator's eyes, run a traditional panel. For everything else — concept screening, ad pre-testing, conjoint, longitudinal sentiment tracking — Panel Studio gives you the iteration speed of code with sentiment data that's been publicly validated against real-world outcomes.

## Part of the Kronaxis research stack

1. [**DYNAMICS-8**](https://github.com/Kronaxis/dynamics-8) — the eight-dimension psychographic framework Panel Studio uses to score every persona
2. **Panel Studio** (this repo) — the engine that simulates 500–65,000 DYNAMICS-tagged personas at a time
3. [**KPM-1**](https://github.com/Kronaxis/kpm1-election-projections) — pre-registered, hash-verified election predictions (the public proof Panel Studio's outputs map to reality)
4. [**Kronaxis Router**](https://github.com/Kronaxis/kronaxis-router) — the LLM proxy that makes running 65,000 simulated personas economically viable

Each piece is independently usable; together they cover the loop from psychographic framework → simulated population → public falsifiable forecast → cost-efficient inference at scale.

## Features

- **Multi turn conversations** with follow-up questions that build on previous responses
- **Conjoint analysis** to test product attributes and price sensitivity across personality segments
- **Focus group synthesis** that generates naturalistic group discussions from individual responses
- **Panel builder** to create new persona panels from demographic specifications
- **Scheduled stimuli** on cron or interval expressions for longitudinal research
- **Export** to JSONL, Parquet, and CSV for training data or downstream analysis
- **Cross-panel comparison** of sentiment across different demographic panels
- **Runs locally** on a local LLM server with no data leaving your machine

## Quick Start

```bash
git clone https://github.com/kronaxis/kronaxis-panel-studio.git
cd kronaxis-panel-studio
cp .env.example .env
```

Set the mandatory values in `.env`:

```bash
echo "TFS_DB_PASSWORD=your_secure_password" >> .env
echo "FLASK_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> .env
echo "OLLAMA_MODEL=qwen2.5:3b" >> .env
```

Then start everything:

```bash
docker-compose up -d
```

The default language model (~2.5 GB) is pulled automatically on first boot. Monitor progress with `docker logs kps-ollama -f`. Once the model is ready, open [http://localhost:8090](http://localhost:8090), navigate to Panels, select "UK Census Panel", and start a conversation.

**GPU recommended.** The LLM server runs on CPU if no GPU is available, but inference will be significantly slower. A GPU with 6 GB+ VRAM is recommended for interactive use.

## API

Panel Studio exposes a REST API for programmatic access. When `PANEL_STUDIO_API_KEY` is not set (the default), all endpoints are open. Set it in `.env` to require an `X-API-Key` header.

For local development, disable the Kronaxis gate (which otherwise requires a free account for exports and panel building):

```
KRONAXIS_GATE_ENABLED=false
```

### List panels

```bash
curl http://localhost:8090/api/panels
```

### Create a conversation and submit a stimulus

```bash
# Get the panel ID from the list above.
PANEL_ID="<your-panel-id>"

# Create a conversation.
CONV=$(curl -s http://localhost:8090/api/panels/$PANEL_ID/conversations \
  -H "Content-Type: application/json" \
  -d '{"title": "Product test"}')

CONV_ID=$(echo $CONV | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Submit a stimulus. Every persona in the panel responds.
curl http://localhost:8090/api/panels/$PANEL_ID/conversations/$CONV_ID/ask \
  -H "Content-Type: application/json" \
  -d '{"stimulus": "A new meal kit service delivers pre-portioned ingredients for 5 meals per week at GBP 45. Would you subscribe? Why or why not?"}'
```

The `/ask` endpoint returns immediately with a `run_id`. Poll `/status` for progress, or open the web UI to watch responses stream in via SSE.

### Check progress

```bash
curl http://localhost:8090/api/panels/$PANEL_ID/conversations/$CONV_ID/status
```

### Export results

```bash
# JSONL (default), CSV, Parquet, or full JSONL with persona metadata.
curl "http://localhost:8090/api/panels/$PANEL_ID/conversations/$CONV_ID/export?format=jsonl" \
  -o responses.jsonl

curl "http://localhost:8090/api/panels/$PANEL_ID/conversations/$CONV_ID/export?format=csv" \
  -o responses.csv
```

## Architecture

```
               +------------------+
               |   Panel Studio   |  Flask, port 8090
               |   (app server)   |
               +--------+---------+
                        |
           +------------+------------+
           |                         |
  +--------v---------+     +---------v--------+
  |   PostgreSQL 15  |     |   LLM Server     |
  |   + pgvector     |     |  (local model)   |
  |   port 5432      |     |  port 11434      |
  +---------+--------+     +------------------+
            |
   500 seed personas
   loaded on first boot
```

| Service | Container | Port | Purpose |
|---|---|---|---|
| `panel-studio` | kps-panel-studio | 8090 | Flask web application and API |
| `ollama` | kps-ollama | 11434 | Local LLM inference server |
| `db` | kps-db | 5432 | PostgreSQL 15 with pgvector |
| `model-pull` | kps-model-pull | -- | One-shot init container; pulls the default model |

**How a stimulus flows:** you submit a question to a panel. Panel Studio loads each persona's DYNAMICS-8 profile, life narrative, and conversation memory into a personalised prompt. The LLM generates a response shaped by that persona's personality. Responses are aggregated by age, gender, region, and personality segment. The result is a demographically broken-down sentiment report with individual-level data available for export.

## DYNAMICS-8

DYNAMICS-8 is an eight-dimension personality framework built for behavioural simulation. It extends Big Five and HEXACO with two dimensions for digital and economic behaviour: Acuity (digital fluency) and Impulsivity (delay discounting). Each dimension is a continuous float from 0.0 to 1.0 with four granular facets, giving 32 behavioural parameters per persona.

| Code | Dimension | What It Predicts |
|------|-----------|------------------|
| **D** | Discipline | Comparison shopping, budget adherence, structured decisions |
| **Y** | Yielding | Endorsement susceptibility, social proof response, compliance |
| **N** | Novelty | Early adoption, brand switching, content diversity |
| **A** | Acuity | Digital campaign engagement, platform behaviour, privacy settings |
| **M** | Mercuriality | Risk aversion, emotional framing response, crisis behaviour |
| **I** | Impulsivity | Purchase speed, notification response, impulse buying |
| **C** | Candour | Authenticity preference, luxury vs value positioning |
| **S** | Sociability | Word-of-mouth amplification, review behaviour, sharing |

The full specification is available at [lib/dynamics/DYNAMICS-8.md](lib/dynamics/DYNAMICS-8.md) and [kronaxis.co.uk/dynamics](https://kronaxis.co.uk/dynamics). The DYNAMICS-8 framework is also available as a standalone library: [github.com/kronaxis/dynamics-8](https://github.com/kronaxis/dynamics-8).

## Data

The 500 pre-loaded personas are the same ungated dataset available on [HuggingFace](https://huggingface.co/kronaxis). Each persona includes full demographics (age, gender, ethnicity, occupation, income, education, location), a DYNAMICS-8 profile, and a life narrative. The distribution is census weighted against ONS 2021 data.

Larger datasets (5,000+ premium personas, 65,000 constituency-level personas, custom countries) are available under commercial licence. See [COMMERCIAL_LICENCE.md](COMMERCIAL_LICENCE.md).

## Configuration

All configuration is via environment variables in `.env`. See [.env.example](.env.example) for the full list.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `TFS_DB_PASSWORD` | Yes | -- | PostgreSQL password |
| `FLASK_SECRET_KEY` | Yes | -- | Flask session secret |
| `OLLAMA_MODEL` | No | (see .env.example) | Language model for persona responses |
| `OLLAMA_BUILD_MODEL` | No | -- | Separate (larger) model for panel building |
| `PANEL_STUDIO_AUTH` | No | `false` | Enable session-based login and multi-tenancy |
| `KRONAXIS_GATE_ENABLED` | No | `true` | Require free Kronaxis account for exports and building |

## Commercial Use

Kronaxis Panel Studio is source-available under the [Business Source Licence 1.1](LICENSE). Free for internal, non-commercial use: research, education, evaluation, and personal projects. Each version converts to Apache 2.0 within 5 years of release.

**A commercial licence is required if you:**
- Use Panel Studio to generate revenue (directly or indirectly)
- Deploy Panel Studio as part of a production service
- Redistribute Panel Studio or create derivative works

Commercial licences are available and include managed cloud API, premium persona datasets, and dedicated support. Contact contact@kronaxis.co.uk for pricing.

## Patents

Panel Studio and DYNAMICS-8 are protected by UK Patent Application GB 2605150.8: "Consumer Behaviour Simulation System", filed 10 March 2026.

## Links

- [kronaxis.co.uk](https://kronaxis.co.uk)
- [DYNAMICS-8 specification](https://kronaxis.co.uk/dynamics)
- [DYNAMICS-8 library](https://github.com/kronaxis/dynamics-8)
- [HuggingFace dataset](https://huggingface.co/kronaxis)
- [Commercial licence](COMMERCIAL_LICENCE.md)
- contact@kronaxis.co.uk

---

<p align="center">
  Built by <strong>Jason Duke</strong>, <a href="https://kronaxis.co.uk">Kronaxis Limited</a>
</p>
