"""
Panel Builder - self-service synthetic persona generation for any country.

Copyright (c) 2026 Kronaxis Limited. All rights reserved.
Licensed under BSL 1.1. See LICENSE file.
https://kronaxis.co.uk | contact@kronaxis.co.uk

Wraps a 3-pass LLM generation pipeline:
  Pass 1: Generate life biography from demographics + DYNAMICS-8
  Pass 2: Populate structured field groups from biography
  Pass 3: Generate questionnaire responses + opinion drift

Uses local Ollama for all LLM calls (no external API keys required).
"""

import asyncio
import json
import logging
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import psycopg2.extras
import requests

try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

log = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_BUILD_MODEL = os.environ.get("OLLAMA_BUILD_MODEL", "") or os.environ.get("OLLAMA_MODEL", "qwen3:4b")
BUILD_CONCURRENCY = int(os.environ.get("BUILD_CONCURRENCY", "5"))

DYNAMICS_DIMS = ["D", "Y", "N", "A", "M", "I", "C", "S"]

# ---------------------------------------------------------------------------
# Free-form description interpreter
# ---------------------------------------------------------------------------

async def enhance_panel_description(raw_description, api_key=None):
    """Expand a brief user description into a rich, detailed research brief.

    Takes terse input like "SEOs at all levels" and returns a thorough
    description covering demographics, seniority range, industry context,
    geographic spread, career paths, and cultural traits.
    """
    prompt = f"""You are a senior market research consultant. A client has given you a brief description of the panel of people they want to study. Your job is to expand this into a rich, detailed research brief that will produce a realistic and representative panel.

Client's brief: "{raw_description}"

Write a 150-250 word expanded description that covers:
1. Who these people are: job titles, roles, seniority levels from entry to senior
2. Industry context: what sector(s) they work in, typical employers (agencies, in-house, freelance, startups, enterprises)
3. Geographic spread: which countries or regions are most relevant and why
4. Demographics: likely age range, education backgrounds (formal vs self-taught), income spread from junior to senior
5. Career paths: how people typically enter this field, common progression routes
6. Professional culture: tools they use, communities they belong to, conferences they attend, publications they read, how they learn and stay current
7. Personality tendencies: what kind of people are drawn to this work (analytical, creative, technical, people-oriented, etc.)
8. Lifestyle factors: work-life balance norms, remote vs office, freelancer proportion, side projects

Write in plain English as a research brief. Be specific with real examples (real tool names, real conference names, real publication names). Do not use bullet points or JSON. Write flowing prose.

Return a JSON object with a single key "enhanced_description" containing the text."""

    raw = await call_ollama(prompt, api_key=api_key, max_output_tokens=2048)
    result = parse_json_response(raw)
    if result and result.get("enhanced_description"):
        return result["enhanced_description"]
    # Fallback: use the raw text if JSON parsing failed but we got text back.
    if raw and len(raw.strip()) > 50:
        return raw.strip()
    return raw_description


async def interpret_panel_description(description, api_key=None, enhance=True):
    """Convert a free-form English description into a structured panel spec.

    When enhance=True (default), first expands the user's brief description
    into a rich research brief, then interprets that into structured fields.
    Returns both the enhanced description and the structured spec.
    """
    enhanced = description
    if enhance:
        enhanced = await enhance_panel_description(description, api_key=api_key)

    prompt = f"""You are a market research panel designer. A user wants to create a synthetic panel of people. Convert their description into a structured specification.

User description: "{enhanced}"

Return a JSON object with:
- "name": suggested panel name (short, descriptive, e.g. "Global SEO Professionals")
- "country": target country name, or "Global" if the description implies worldwide/multiple countries
- "panel_size": suggested number of personas (50-500, based on how much variety the description implies)
- "demographic_spec": object with any relevant filters from this list (omit fields that should use defaults):
  - "age_min": int (minimum age)
  - "age_max": int (maximum age)
  - "genders": list of genders to include (omit for both)
  - "education_levels": list of education levels to include
  - "employment_statuses": list of employment statuses
  - "social_classes": list of social classes
  - "urban_rural": list of settings ("Urban", "Suburban", "Rural")
  - "income_min": int (minimum annual income in local currency)
  - "income_max": int (maximum annual income in local currency)
  - "occupation_sectors": list of sector names
  - "has_children": true/false (omit for any)
- "custom_occupations": list of specific occupation entries, each as [title, sector, median_salary, min_salary, max_salary]. Include 8-20 occupations that represent the range described. Salaries in USD for Global, or local currency for single-country panels. This is the most important field for niche panels.
- "persona_guidance": 2-4 sentences of context that should be injected into each persona's biography generation. Describe what makes these people distinctive: their daily work, industry culture, typical career paths, common interests, professional communities, tools they use. This is free text that enriches the biography.
- "dynamics_biases": object with any DYNAMICS-8 dimensions that should be biased for this population. Keys are D/Y/N/A/M/I/C/S, values are objects with "min" and "max" (0.0 to 1.0). Only include dimensions where this population differs from general population. D=Discipline, Y=Yielding, N=Novelty, A=Analytical, M=Morality, I=Intensity, C=Caution, S=Sociability.

Be specific and realistic. For custom_occupations, include the full range of seniority levels and specialisations implied by the description. For persona_guidance, focus on what would make the biographies authentic and distinctive for this population."""

    raw = await call_ollama(prompt, api_key=api_key, max_output_tokens=4096)
    result = parse_json_response(raw)
    if not result:
        raise RuntimeError("Failed to interpret panel description")
    if enhance:
        result["enhanced_description"] = enhanced
    return result


PANEL_PRESETS = {
    "young_professionals": {
        "label": "Young Professionals",
        "description": "Urban degree-educated workers aged 25-35",
        "spec": {"age_min": 25, "age_max": 35, "education_levels": ["Degree", "Postgraduate"],
                 "urban_rural": ["Urban"], "employment_statuses": ["Employed full-time", "Self-employed"]},
    },
    "retirees": {
        "label": "Retirees",
        "description": "Retired population aged 65+",
        "spec": {"age_min": 65, "age_max": 90, "employment_statuses": ["Retired"]},
    },
    "working_class": {
        "label": "Working Class",
        "description": "Manual and service sector workers",
        "spec": {"social_classes": ["Working", "Lower middle"],
                 "education_levels": ["No qualifications", "GCSE or equivalent", "Apprenticeship", "A-level or equivalent"]},
    },
    "parents": {
        "label": "Parents with Children",
        "description": "Adults aged 25-55 with children",
        "spec": {"age_min": 25, "age_max": 55, "has_children": True},
    },
    "students": {
        "label": "Students",
        "description": "Full-time students aged 18-25",
        "spec": {"age_min": 18, "age_max": 25, "employment_statuses": ["Student"]},
    },
    "high_income": {
        "label": "High Income",
        "description": "Earners above 75th percentile",
        "spec": {"income_min": 75000, "education_levels": ["Degree", "Postgraduate"]},
    },
    "rural_communities": {
        "label": "Rural Communities",
        "description": "People living in rural areas",
        "spec": {"urban_rural": ["Rural"]},
    },
    "immigrants": {
        "label": "First-Gen Immigrants",
        "description": "First-generation immigrants",
        "spec": {"immigration_statuses": ["first_generation"]},
    },
    "renters": {
        "label": "Renters",
        "description": "Private and social renters",
        "spec": {"housing_tenures": ["Private renter", "Social renter"]},
    },
    "millennials": {
        "label": "Millennials",
        "description": "Born 1981-1996 (aged 30-45)",
        "spec": {"age_min": 30, "age_max": 45},
    },
    "gen_z": {
        "label": "Gen Z",
        "description": "Born 1997-2012 (aged 14-29)",
        "spec": {"age_min": 18, "age_max": 29},
    },
    "women": {
        "label": "Women",
        "description": "Female respondents only",
        "spec": {"genders": ["female"]},
    },
}

# ---------------------------------------------------------------------------
# In-memory job tracker (for progress polling)
# ---------------------------------------------------------------------------

_active_jobs = {}
_jobs_lock = threading.Lock()


def get_job_progress(job_id):
    with _jobs_lock:
        return _active_jobs.get(str(job_id))


def _update_progress(job_id, current, total, pass_name):
    with _jobs_lock:
        _active_jobs[str(job_id)] = {
            "current": current,
            "total": total,
            "pass": pass_name,
        }


def _clear_progress(job_id):
    with _jobs_lock:
        _active_jobs.pop(str(job_id), None)


# ---------------------------------------------------------------------------
# Ollama API client (async)
# ---------------------------------------------------------------------------

async def call_ollama(prompt, api_key=None, max_retries=5, temperature=0.8,
                      max_output_tokens=4096, json_mode=True):
    """Call Ollama API with exponential backoff.

    Named call_ollama for compatibility with the 3-pass pipeline callers.
    Uses Ollama's /api/generate endpoint for single-prompt generation.
    """
    if not _HAS_AIOHTTP:
        raise RuntimeError("aiohttp required: pip install aiohttp")

    url = OLLAMA_URL.rstrip("/") + "/api/generate"
    model = OLLAMA_BUILD_MODEL

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": min(max_output_tokens, 4096),
        },
    }

    if json_mode:
        payload["format"] = "json"

    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=180)
                ) as resp:
                    if resp.status == 503:
                        wait = min(2 ** attempt * 4, 60) + random.uniform(0, 2)
                        log.warning("Ollama busy, retrying in %.1fs (attempt %d)", wait, attempt + 1)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status != 200:
                        text = await resp.text()
                        raise RuntimeError(f"Ollama API error {resp.status}: {text[:300]}")
                    data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(min(2 ** attempt * 2, 30))
                continue
            raise

        content = data.get("response", "")
        # Strip Qwen3 think tags if present.
        if "<think>" in content:
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        return content

    raise RuntimeError("Ollama API: max retries exceeded")


def parse_json_response(raw):
    """Parse JSON from Ollama response, handling markdown fences."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


# ---------------------------------------------------------------------------
# Demographic profile generation (country-agnostic)
# ---------------------------------------------------------------------------

UK_REGIONS = {
    "South East": 0.145, "London": 0.134, "North West": 0.116,
    "East of England": 0.099, "West Midlands": 0.094, "South West": 0.089,
    "Yorkshire and The Humber": 0.087, "East Midlands": 0.077,
    "North East": 0.042, "Wales": 0.050, "Scotland": 0.087,
    "Northern Ireland": 0.030,
}


async def generate_demographic_profile(country, count, demographic_spec=None):
    """Generate a demographic profile for persona generation.

    For UK: returns census-weighted defaults.
    For other countries: asks Ollama to generate plausible distributions.
    """
    if country.lower() in ("united kingdom", "uk", "britain", "great britain"):
        return _uk_profile(count, demographic_spec)

    prompt = f"""Generate a realistic demographic profile for {count} synthetic personas representing the general population of {country}.

Return a JSON object with:
- "regions": dict of region_name -> proportion (must sum to 1.0), covering 8-15 major regions/states/provinces
- "towns_by_region": dict of region_name -> list of 5-10 representative towns/cities
- "age_bands": dict of "[min, max]" -> proportion (must sum to 1.0), e.g. "[18, 24]": 0.12
- "genders": {{"male": proportion, "female": proportion}}
- "ethnicities": dict of ethnicity -> proportion for the country
- "occupations": list of 50+ common occupations as [title, sector, median_salary_usd, min_salary_usd, max_salary_usd]
- "political_parties": list of major party names
- "religions": dict of religion -> proportion
- "currency_symbol": string (e.g. "$", "EUR", "GBP")
- "common_first_names_male": list of 50 common male first names
- "common_first_names_female": list of 50 common female first names
- "common_surnames": list of 100 common surnames
- "education_levels": dict of level_name -> proportion for the country's education system
- "employment_statuses": dict of status -> proportion (employed full-time, part-time, self-employed, unemployed, student, retired, homemaker)
- "marital_statuses": dict of status -> proportion
- "housing_tenures": dict of tenure_type -> proportion
- "languages": dict of language_or_combo -> proportion (e.g. "French only": 0.7, "French and Arabic": 0.1)
- "social_classes": dict of class -> proportion (upper, upper middle, middle, lower middle, working, underclass)
- "urban_rural": dict of setting -> proportion (urban, suburban, rural)
- "disability_rate": float (proportion of population with disability)
- "immigration": dict of status -> proportion (native, first_generation, second_generation)
- "immigration_origins": list of 10-15 most common origin countries
- "household_sizes": dict of size -> proportion ("1", "2", "3", "4", "5+")
- "occupation_sectors": list of 15-20 common employment sectors

Base distributions on real census/demographic data for {country}. Be accurate."""

    if demographic_spec:
        overrides = json.dumps(demographic_spec, indent=2)
        prompt += f"\n\nApply these overrides to the distributions:\n{overrides}"

    raw = await call_ollama(prompt, max_output_tokens=8192)
    profile = parse_json_response(raw)
    if not profile:
        raise RuntimeError(f"Failed to generate demographic profile for {country}")

    profile["country"] = country
    return profile


def _uk_profile(count, demographic_spec=None):
    """Return UK census-weighted profile (hardcoded, no API call needed)."""
    return {
        "country": "United Kingdom",
        "regions": UK_REGIONS,
        "towns_by_region": {
            "South East": ["Brighton", "Southampton", "Oxford", "Reading", "Canterbury", "Guildford"],
            "London": ["Camden", "Greenwich", "Hackney", "Islington", "Southwark", "Lewisham"],
            "North West": ["Manchester", "Liverpool", "Preston", "Bolton", "Blackpool", "Chester"],
            "East of England": ["Norwich", "Cambridge", "Ipswich", "Colchester", "Chelmsford"],
            "West Midlands": ["Birmingham", "Coventry", "Wolverhampton", "Stoke-on-Trent"],
            "South West": ["Bristol", "Plymouth", "Exeter", "Bath", "Cheltenham"],
            "Yorkshire and The Humber": ["Leeds", "Sheffield", "Bradford", "Hull", "York"],
            "East Midlands": ["Nottingham", "Leicester", "Derby", "Northampton"],
            "North East": ["Newcastle", "Sunderland", "Middlesbrough", "Durham"],
            "Wales": ["Cardiff", "Swansea", "Newport", "Wrexham", "Bangor"],
            "Scotland": ["Edinburgh", "Glasgow", "Aberdeen", "Dundee", "Inverness"],
            "Northern Ireland": ["Belfast", "Derry", "Lisburn", "Newry"],
        },
        "age_bands": {"[18, 24]": 0.10, "[25, 34]": 0.17, "[35, 44]": 0.16,
                       "[45, 54]": 0.17, "[55, 64]": 0.15, "[65, 74]": 0.13, "[75, 90]": 0.12},
        "genders": {"male": 0.49, "female": 0.51},
        "ethnicities": {"White British": 0.74, "White Other": 0.06, "Asian": 0.08,
                         "Black": 0.04, "Mixed": 0.03, "Chinese": 0.01, "Other": 0.04},
        "political_parties": ["Conservative", "Labour", "Liberal Democrat", "Green",
                               "Reform UK", "SNP", "Plaid Cymru"],
        "religions": {"Christian": 0.46, "No religion": 0.37, "Muslim": 0.07,
                       "Hindu": 0.02, "Sikh": 0.01, "Jewish": 0.01, "Buddhist": 0.01, "Other": 0.05},
        "currency_symbol": "GBP",
        "common_first_names_male": ["James", "John", "Robert", "David", "William", "Thomas",
                                     "Daniel", "Matthew", "Oliver", "Jack", "Harry", "George",
                                     "Charlie", "Jacob", "Noah", "Oscar", "Leo", "Arthur"],
        "common_first_names_female": ["Mary", "Sarah", "Emma", "Charlotte", "Olivia", "Amelia",
                                       "Jessica", "Sophie", "Emily", "Grace", "Lily", "Mia",
                                       "Isabella", "Ella", "Chloe", "Hannah", "Lucy", "Ruby"],
        "common_surnames": ["Smith", "Jones", "Williams", "Taylor", "Brown", "Davies", "Wilson",
                             "Evans", "Thomas", "Johnson", "Roberts", "Walker", "Wright", "Robinson",
                             "Thompson", "White", "Hughes", "Edwards", "Green", "Hall", "Lewis",
                             "Harris", "Clarke", "Patel", "Jackson", "Wood", "Turner", "Martin",
                             "Cooper", "Hill", "Ward", "Morris", "Moore", "Clark", "Lee", "King",
                             "Baker", "Harrison", "Morgan", "Allen", "James", "Scott", "Phillips",
                             "Watson", "Davis", "Parker", "Price", "Bennett", "Young", "Griffiths"],
        "education_levels": {"No qualifications": 0.18, "GCSE or equivalent": 0.20,
                              "A-level or equivalent": 0.17, "Apprenticeship": 0.05,
                              "Degree": 0.27, "Postgraduate": 0.13},
        "employment_statuses": {"Employed full-time": 0.50, "Employed part-time": 0.13,
                                 "Self-employed": 0.10, "Unemployed": 0.04,
                                 "Student": 0.06, "Retired": 0.15, "Homemaker": 0.02},
        "marital_statuses": {"Single": 0.35, "Married": 0.42, "Cohabiting": 0.09,
                              "Divorced": 0.08, "Widowed": 0.05, "Separated": 0.01},
        "housing_tenures": {"Owner (outright)": 0.31, "Owner (mortgage)": 0.28,
                             "Private renter": 0.20, "Social renter": 0.17,
                             "Living with family": 0.04},
        "languages": {"English only": 0.87, "English and Polish": 0.02,
                       "English and Urdu": 0.01, "English and Punjabi": 0.01,
                       "English and Bengali": 0.01, "English and Arabic": 0.01,
                       "English and Gujarati": 0.01, "English and Welsh": 0.02,
                       "English and French": 0.01, "English and Portuguese": 0.01,
                       "English and Spanish": 0.01, "English and Other": 0.01},
        "social_classes": {"Upper": 0.02, "Upper middle": 0.12, "Middle": 0.30,
                            "Lower middle": 0.25, "Working": 0.25, "Underclass": 0.06},
        "urban_rural": {"Urban": 0.56, "Suburban": 0.27, "Rural": 0.17},
        "disability_rate": 0.18,
        "immigration": {"native": 0.86, "first_generation": 0.09, "second_generation": 0.05},
        "immigration_origins": ["Poland", "India", "Pakistan", "Romania", "Ireland",
                                 "Nigeria", "Bangladesh", "China", "Philippines", "South Africa"],
        "household_sizes": {"1": 0.30, "2": 0.34, "3": 0.16, "4": 0.13, "5+": 0.07},
        "occupation_sectors": ["Healthcare", "Education", "Technology", "Finance", "Retail",
                                "Construction", "Manufacturing", "Transport", "Hospitality",
                                "Public administration", "Legal", "Media", "Agriculture",
                                "Energy", "Arts", "Science", "Social work", "Military"],
    }


# ---------------------------------------------------------------------------
# Country options for UI (cached)
# ---------------------------------------------------------------------------

_COUNTRY_OPTIONS_CACHE = {}

async def get_country_options(country):
    """Return available filter options for a country's demographic fields.

    For UK: hardcoded. For others: Ollama-generated and cached.
    """
    key = country.lower().strip()
    if key in _COUNTRY_OPTIONS_CACHE:
        return _COUNTRY_OPTIONS_CACHE[key]

    if key in ("united kingdom", "uk", "britain", "great britain"):
        opts = _uk_options()
        _COUNTRY_OPTIONS_CACHE[key] = opts
        return opts

    # Generate options via Ollama for any other country.
    prompt = f"""For {country}, provide lists of options for building a demographic survey panel.

Return a JSON object with these keys:
- "regions": list of 8-20 major regions/states/provinces/departments
- "ethnicities": list of 8-15 ethnic/racial groups in that country
- "political_parties": list of 5-10 major political parties
- "religions": list of 6-10 religious groups (include "No religion")
- "languages": list of 8-15 languages commonly spoken (include bilingual combos)
- "occupation_sectors": list of 15-20 employment sectors
- "education_levels": list of 5-8 education levels (country-specific names, e.g. Baccalauréat for France, High School Diploma for US)
- "employment_statuses": list of 6-8 statuses (employed, self-employed, unemployed, student, retired, homemaker, etc.)
- "marital_statuses": list of 5-6 options
- "housing_tenures": list of 4-6 housing types
- "social_classes": list of 5-6 socioeconomic tiers
- "immigration_origins": list of 10-15 most common origin countries for immigrants
- "currency_code": ISO 4217 code (e.g. "USD", "EUR", "GBP")
- "currency_symbol": symbol (e.g. "$", "€", "£")

Use real demographic data for {country}. Lists should reflect actual population composition."""

    raw = await call_ollama(prompt, max_output_tokens=4096)
    opts = parse_json_response(raw)
    if not opts:
        opts = _generic_options(country)

    opts["country"] = country
    _COUNTRY_OPTIONS_CACHE[key] = opts
    return opts


def _uk_options():
    """Hardcoded UK filter options."""
    return {
        "country": "United Kingdom",
        "regions": list(UK_REGIONS.keys()),
        "ethnicities": ["White British", "White Other", "Asian", "Black", "Mixed", "Chinese", "Other"],
        "political_parties": ["Conservative", "Labour", "Liberal Democrat", "Green", "Reform UK", "SNP", "Plaid Cymru"],
        "religions": ["Christian", "No religion", "Muslim", "Hindu", "Sikh", "Jewish", "Buddhist", "Other"],
        "languages": ["English only", "English and Welsh", "English and Polish", "English and Urdu",
                       "English and Punjabi", "English and Bengali", "English and Arabic",
                       "English and Gujarati", "English and French", "English and Portuguese",
                       "English and Spanish", "English and Other"],
        "occupation_sectors": ["Healthcare", "Education", "Technology", "Finance", "Retail",
                                "Construction", "Manufacturing", "Transport", "Hospitality",
                                "Public administration", "Legal", "Media", "Agriculture",
                                "Energy", "Arts", "Science", "Social work", "Military",
                                "Property", "Telecommunications"],
        "education_levels": ["No qualifications", "GCSE or equivalent", "A-level or equivalent",
                              "Apprenticeship", "Degree", "Postgraduate"],
        "employment_statuses": ["Employed full-time", "Employed part-time", "Self-employed",
                                 "Unemployed", "Student", "Retired", "Homemaker"],
        "marital_statuses": ["Single", "Married", "Cohabiting", "Divorced", "Widowed", "Separated"],
        "housing_tenures": ["Owner (outright)", "Owner (mortgage)", "Private renter",
                             "Social renter", "Living with family"],
        "social_classes": ["Upper", "Upper middle", "Middle", "Lower middle", "Working", "Underclass"],
        "immigration_origins": ["Poland", "India", "Pakistan", "Romania", "Ireland", "Nigeria",
                                 "Bangladesh", "China", "Philippines", "South Africa", "Jamaica",
                                 "Italy", "Portugal", "Lithuania", "Germany"],
        "currency_code": "GBP",
        "currency_symbol": "£",
    }


def _generic_options(country):
    """Fallback options if Ollama call fails."""
    return {
        "country": country,
        "regions": [],
        "ethnicities": [],
        "political_parties": [],
        "religions": ["Christian", "Muslim", "Hindu", "Buddhist", "Jewish", "No religion", "Other"],
        "languages": [],
        "occupation_sectors": ["Healthcare", "Education", "Technology", "Finance", "Retail",
                                "Construction", "Manufacturing", "Transport", "Hospitality",
                                "Public administration", "Legal", "Media", "Agriculture"],
        "education_levels": ["No formal education", "Primary", "Secondary", "Vocational",
                              "Bachelor's degree", "Master's degree", "Doctorate"],
        "employment_statuses": ["Employed full-time", "Employed part-time", "Self-employed",
                                 "Unemployed", "Student", "Retired", "Homemaker"],
        "marital_statuses": ["Single", "Married", "Cohabiting", "Divorced", "Widowed"],
        "housing_tenures": ["Owner", "Renter", "Social housing", "Living with family"],
        "social_classes": ["Upper", "Upper middle", "Middle", "Lower middle", "Working"],
        "immigration_origins": [],
        "currency_code": "USD",
        "currency_symbol": "$",
    }


# ---------------------------------------------------------------------------
# Skeleton generation
# ---------------------------------------------------------------------------

def generate_skeletons(profile, count, seed=42, spec=None):
    """Generate persona skeletons from a demographic profile with optional filters."""
    rng = random.Random(seed)
    spec = spec or {}
    country = profile.get("country", "Unknown")

    # Extract distributions from profile.
    regions = dict(profile.get("regions", {}))
    towns = profile.get("towns_by_region", {})
    age_bands = dict(profile.get("age_bands", {}))
    genders = dict(profile.get("genders", {"male": 0.49, "female": 0.51}))
    ethnicities = dict(profile.get("ethnicities", {"Unspecified": 1.0}))
    occupations = profile.get("occupations", [])
    names_m = profile.get("common_first_names_male", ["Alex"])
    names_f = profile.get("common_first_names_female", ["Sam"])
    surnames = profile.get("common_surnames", ["Smith"])
    education_levels = dict(profile.get("education_levels", {"Secondary": 0.40, "Tertiary": 0.35, "Primary": 0.15, "Postgraduate": 0.10}))
    employment_statuses = dict(profile.get("employment_statuses", {"Employed full-time": 0.55, "Employed part-time": 0.10, "Self-employed": 0.10, "Unemployed": 0.05, "Student": 0.05, "Retired": 0.12, "Homemaker": 0.03}))
    marital_statuses = dict(profile.get("marital_statuses", {"Single": 0.35, "Married": 0.45, "Divorced": 0.10, "Widowed": 0.05, "Cohabiting": 0.05}))
    housing_tenures = dict(profile.get("housing_tenures", {"Owner": 0.40, "Renter": 0.35, "Social housing": 0.15, "Living with family": 0.10}))
    languages = dict(profile.get("languages", {"English only": 1.0}))
    social_classes = dict(profile.get("social_classes", {"Upper": 0.02, "Upper middle": 0.12, "Middle": 0.30, "Lower middle": 0.25, "Working": 0.25, "Underclass": 0.06}))
    urban_rural = dict(profile.get("urban_rural", {"Urban": 0.55, "Suburban": 0.25, "Rural": 0.20}))
    religions = dict(profile.get("religions", {"No religion": 1.0}))
    disability_rate = profile.get("disability_rate", 0.15)
    immigration = dict(profile.get("immigration", {"native": 0.90, "first_generation": 0.07, "second_generation": 0.03}))
    immigration_origins = profile.get("immigration_origins", [])
    household_sizes = dict(profile.get("household_sizes", {"1": 0.28, "2": 0.34, "3": 0.16, "4": 0.14, "5+": 0.08}))
    occupation_sectors = profile.get("occupation_sectors", [])
    political_parties = profile.get("political_parties", [])

    # Apply spec filters: narrow distributions to only allowed values.
    def filter_dist(dist, allowed):
        """Keep only keys in allowed list, renormalise."""
        filtered = {k: v for k, v in dist.items() if k in allowed}
        if not filtered:
            return dist
        total = sum(filtered.values())
        return {k: v / total for k, v in filtered.items()} if total > 0 else dist

    if spec.get("regions"):
        regions = filter_dist(regions, spec["regions"])
    if spec.get("genders"):
        genders = filter_dist(genders, [g.lower() for g in spec["genders"]])
        # Also try capitalised
        genders2 = filter_dist(dict(profile.get("genders", {})), spec["genders"])
        if len(genders2) > 0 and sum(genders2.values()) > 0:
            genders = genders2
    if spec.get("ethnicities"):
        ethnicities = filter_dist(ethnicities, spec["ethnicities"])
    if spec.get("education_levels"):
        education_levels = filter_dist(education_levels, spec["education_levels"])
    if spec.get("employment_statuses"):
        employment_statuses = filter_dist(employment_statuses, spec["employment_statuses"])
    if spec.get("marital_statuses"):
        marital_statuses = filter_dist(marital_statuses, spec["marital_statuses"])
    if spec.get("housing_tenures"):
        housing_tenures = filter_dist(housing_tenures, spec["housing_tenures"])
    if spec.get("languages"):
        languages = filter_dist(languages, spec["languages"])
    if spec.get("social_classes"):
        social_classes = filter_dist(social_classes, spec["social_classes"])
    if spec.get("urban_rural"):
        urban_rural = filter_dist(urban_rural, spec["urban_rural"])
    if spec.get("religions"):
        religions = filter_dist(religions, spec["religions"])
    if spec.get("immigration_statuses"):
        immigration = filter_dist(immigration, spec["immigration_statuses"])
    if spec.get("political_parties"):
        political_parties = [p for p in political_parties if p in spec["political_parties"]]
        if not political_parties:
            political_parties = spec["political_parties"]

    # Age range filter: adjust age bands.
    age_min = spec.get("age_min", 0)
    age_max = spec.get("age_max", 120)
    if age_min > 0 or age_max < 120:
        filtered_bands = {}
        for band_key, prop in age_bands.items():
            try:
                bounds = json.loads(band_key) if isinstance(band_key, str) else band_key
                if bounds[1] >= age_min and bounds[0] <= age_max:
                    new_min = max(bounds[0], age_min)
                    new_max = min(bounds[1], age_max)
                    filtered_bands[json.dumps([new_min, new_max])] = prop
            except (json.JSONDecodeError, TypeError, IndexError):
                filtered_bands[band_key] = prop
        if filtered_bands:
            total = sum(filtered_bands.values())
            age_bands = {k: v / total for k, v in filtered_bands.items()} if total > 0 else age_bands

    income_min = spec.get("income_min", 0)
    income_max = spec.get("income_max", 0)
    has_children_filter = spec.get("has_children")
    disability_filter = spec.get("disability")
    household_min = spec.get("household_size_min", 0)
    household_max = spec.get("household_size_max", 0)

    if spec.get("occupation_sectors") and occupation_sectors:
        occupation_sectors = [s for s in occupation_sectors if s in spec["occupation_sectors"]]
        if not occupation_sectors:
            occupation_sectors = spec["occupation_sectors"]

    # Custom occupations override: use spec-provided occupation list if present.
    custom_occupations = spec.get("custom_occupations")
    if custom_occupations and isinstance(custom_occupations, list) and len(custom_occupations) > 0:
        occupations = custom_occupations

    # DYNAMICS-8 biases: narrow random range for specified dimensions.
    dynamics_biases = spec.get("dynamics_biases", {})

    def weighted_choice(opts):
        items = list(opts.keys())
        weights = list(opts.values())
        return rng.choices(items, weights=weights, k=1)[0]

    # Pre-assign regions proportionally.
    region_list = []
    for region, prop in regions.items():
        region_list.extend([region] * max(1, round(count * prop)))
    while len(region_list) < count:
        region_list.append(rng.choice(list(regions.keys())))
    region_list = region_list[:count]
    rng.shuffle(region_list)

    skeletons = []
    for i in range(count):
        pid = f"KX-{i + 1:05d}"
        region = region_list[i]
        town_list = towns.get(region, [region])
        town = rng.choice(town_list) if town_list else region

        gender = weighted_choice(genders)
        ethnicity = weighted_choice(ethnicities)

        # Age.
        if age_bands:
            band_key = weighted_choice(age_bands)
            try:
                bounds = json.loads(band_key) if isinstance(band_key, str) else band_key
                age = rng.randint(bounds[0], bounds[1])
            except (json.JSONDecodeError, TypeError, IndexError):
                age = rng.randint(18, 75)
        else:
            age = rng.randint(max(18, age_min), min(90, age_max) if age_max > 0 else 90)

        # Name.
        if gender.lower() == "male":
            first_name = rng.choice(names_m)
        else:
            first_name = rng.choice(names_f)
        surname = rng.choice(surnames)

        # Occupation.
        if occupations and isinstance(occupations[0], list):
            filtered_occ = occupations
            if occupation_sectors:
                sector_occ = [o for o in occupations if len(o) > 1 and o[1] in occupation_sectors]
                if sector_occ:
                    filtered_occ = sector_occ
            occ = rng.choice(filtered_occ)
            occ_title, occ_sector = occ[0], occ[1]
            salary_min = occ[3] if len(occ) > 3 else 20000
            salary_max = occ[4] if len(occ) > 4 else 60000
        else:
            occ_title = rng.choice(occupations) if occupations else "Worker"
            occ_sector = rng.choice(occupation_sectors) if occupation_sectors else "General"
            salary_min, salary_max = 20000, 60000

        # Employment status.
        emp_status = weighted_choice(employment_statuses)
        if age >= 67 and "Retired" in employment_statuses:
            emp_status = "Retired"
            occ_title = "Retired"
            occ_sector = "Retired"
            salary_min, salary_max = 10000, 30000

        if emp_status == "Retired":
            occ_title = "Retired"
            occ_sector = "Retired"
            salary_min, salary_max = 10000, 30000
        elif emp_status == "Student":
            occ_title = "Student"
            occ_sector = "Education"
            salary_min, salary_max = 0, 15000
        elif emp_status == "Unemployed":
            salary_min, salary_max = 0, 20000
        elif emp_status == "Homemaker":
            occ_title = "Homemaker"
            occ_sector = "Domestic"
            salary_min, salary_max = 0, 10000

        income = rng.randint(int(salary_min), int(salary_max))
        if income_min > 0:
            income = max(income, income_min)
        if income_max > 0:
            income = min(income, income_max)

        # New demographic fields.
        education = weighted_choice(education_levels)
        marital = weighted_choice(marital_statuses)
        housing = weighted_choice(housing_tenures)
        language = weighted_choice(languages)
        social_class = weighted_choice(social_classes)
        setting = weighted_choice(urban_rural)
        religion = weighted_choice(religions)
        political = rng.choice(political_parties) if political_parties else ""

        # Immigration.
        imm_status = weighted_choice(immigration)
        imm_origin = ""
        if imm_status in ("first_generation", "second_generation") and immigration_origins:
            imm_origin = rng.choice(immigration_origins)

        # Disability.
        has_disability = rng.random() < disability_rate
        if disability_filter is True:
            has_disability = True
        elif disability_filter is False:
            has_disability = False

        # Household.
        hh_size_key = weighted_choice(household_sizes)
        if hh_size_key == "5+":
            hh_size = rng.randint(5, 8)
        else:
            hh_size = int(hh_size_key)
        if household_min > 0:
            hh_size = max(hh_size, household_min)
        if household_max > 0:
            hh_size = min(hh_size, household_max)

        # Children.
        if has_children_filter is True:
            has_children = True
            num_children = rng.randint(1, 4)
        elif has_children_filter is False:
            has_children = False
            num_children = 0
        else:
            if marital in ("Married", "Cohabiting") and age >= 25:
                has_children = rng.random() < 0.65
            elif age >= 30:
                has_children = rng.random() < 0.40
            else:
                has_children = rng.random() < 0.10
            num_children = rng.randint(1, 3) if has_children else 0

        dynamics = {}
        for dim in DYNAMICS_DIMS:
            bias = dynamics_biases.get(dim, {})
            d_min = bias.get("min", 0.0)
            d_max = bias.get("max", 1.0)
            dynamics[dim] = round(rng.uniform(d_min, d_max), 2)

        skeletons.append({
            "persona_id": pid,
            "system_age_days": rng.randint(7, 365),
            "identity": {
                "first_name": first_name,
                "surname": surname,
                "age": age,
                "gender": gender,
                "ethnicity": ethnicity,
                "region": region,
                "town": town,
                "country": country,
                "occupation": occ_title,
                "occupation_sector": occ_sector,
                "annual_income": income,
                "education_level": education,
                "employment_status": emp_status,
                "marital_status": marital,
                "housing_tenure": housing,
                "languages": language,
                "social_class": social_class,
                "urban_rural": setting,
                "religion": religion,
                "political_leaning": political,
                "immigration_status": imm_status,
                "immigration_origin": imm_origin,
                "has_disability": has_disability,
                "household_size": hh_size,
                "has_children": has_children,
                "num_children": num_children,
            },
            "dynamics_8": dynamics,
        })

    return skeletons


# ---------------------------------------------------------------------------
# 3-pass generation prompts
# ---------------------------------------------------------------------------

def _pass1_prompt(skeleton, persona_guidance=None):
    """Build Pass 1 prompt: generate biography from demographics."""
    identity = skeleton["identity"]
    dyn = skeleton["dynamics_8"]
    dyn_desc = ", ".join(f"{k}={v}" for k, v in dyn.items())

    guidance_block = ""
    if persona_guidance:
        guidance_block = f"""
Panel context (use this to inform the biography, making it authentic to this population):
{persona_guidance}

"""

    return f"""You are creating a detailed fictional biography for a synthetic persona.
{guidance_block}Demographics:
- Name: {identity.get('first_name', 'Alex')} {identity.get('surname', 'Smith')}
- Age: {identity['age']}, Gender: {identity['gender']}, Ethnicity: {identity.get('ethnicity', 'unspecified')}
- Location: {identity.get('town', '')}, {identity.get('region', '')}, {identity.get('country', 'UK')} ({identity.get('urban_rural', '')})
- Occupation: {identity['occupation']} ({identity.get('occupation_sector', '')})
- Employment: {identity.get('employment_status', '')}
- Annual income: {identity.get('annual_income', '')}
- Education: {identity.get('education_level', '')}
- Marital status: {identity.get('marital_status', '')}, Household size: {identity.get('household_size', '')}, Children: {identity.get('num_children', 0)}
- Housing: {identity.get('housing_tenure', '')}
- Religion: {identity.get('religion', '')}
- Languages: {identity.get('languages', '')}
- Political leaning: {identity.get('political_leaning', '')}
- Immigration: {identity.get('immigration_status', 'native')}{f", origin: {identity['immigration_origin']}" if identity.get('immigration_origin') else ''}
- Social class: {identity.get('social_class', '')}
- Disability: {'yes' if identity.get('has_disability') else 'no'}
- DYNAMICS-8 personality profile: {dyn_desc}
  (D=Discipline, Y=Yielding, N=Novelty, A=Analytical, M=Morality, I=Intensity, C=Caution, S=Sociability)

Write a 400-600 word biography covering:
1. Family background and upbringing
2. Education and career path
3. Key life events and turning points
4. Current lifestyle, relationships, and daily routines
5. Personality traits consistent with the DYNAMICS-8 profile
6. Financial situation and housing
7. Political views and civic engagement
8. Hobbies, interests, and social life

Return a JSON object with a single key "biography" containing the full text."""


def _pass2_prompt(skeleton, biography):
    """Build Pass 2 prompt: extract structured fields from biography."""
    identity = skeleton["identity"]
    dyn = skeleton["dynamics_8"]

    return f"""Given this persona biography and demographics, populate ALL structured fields.

Name: {identity.get('first_name', '')} {identity.get('surname', '')}
Age: {identity['age']}, Gender: {identity['gender']}
Location: {identity.get('town', '')}, {identity.get('region', '')}, {identity.get('country', 'UK')}
Occupation: {identity['occupation']}
DYNAMICS-8: {json.dumps(dyn)}

Biography:
{biography}

Return a JSON object with these exact top-level keys:
- "identity": {{first_name, surname, age, gender, ethnicity, region, town, country, education_level, occupation, occupation_sector, annual_income, housing_type, household_composition}}
- "political": {{party_affiliation, engagement_level, key_issues: [str], voting_history: [{{year, election, party_voted, reason}}], political_drift: [{{year, from, to, trigger_event}}]}}
- "financial": {{annual_income, housing_status, credit_score_band, savings_behaviour, price_sensitivity, risk_tolerance, financial_anxiety, debt_level}}
- "beliefs": {{worldview_summary, core_values: [str], moral_foundations: {{care, fairness, loyalty, authority, sanctity}}}}
- "emotional_state": {{baseline_mood, emotional_volatility, resilience_rating, stress_triggers: [str], coping_mechanisms: [str]}}
- "relationships": [{{name, relationship, closeness, description}}] (3-5 key relationships)
- "lifecycle": {{life_stage, formative_experiences: [str], aspirations_short, aspirations_long, regrets}}
- "religious_cultural": {{faith, practice_level, cultural_identity}}
- "memory": {{episodic: [str]}} (4-6 vivid personal memories)

Ensure all fields are consistent with the biography and DYNAMICS-8 profile."""


def _pass3_prompt(skeleton, biography, structured):
    """Build Pass 3 prompt: generate questionnaire responses."""
    identity = skeleton["identity"]
    dyn = skeleton["dynamics_8"]

    return f"""Given this persona's biography and structured profile, generate questionnaire responses.

Name: {identity.get('first_name', '')} {identity.get('surname', '')}
Age: {identity['age']}, Location: {identity.get('country', 'UK')}
DYNAMICS-8: {json.dumps(dyn)}
Political: {json.dumps(structured.get('political', {}))}
Beliefs: {json.dumps(structured.get('beliefs', {}))}

Biography excerpt: {biography[:500]}

Return a JSON object with key "questionnaire" containing responses to these topics (each with "position" (1-5 scale) and "reasoning" (1-2 sentences)):
- "immigration": attitude to immigration policy
- "environment": priority of environmental protection vs economic growth
- "inequality": view on wealth redistribution
- "technology": trust in technology companies
- "healthcare": preference for public vs private healthcare
- "education": view on education funding priorities
- "crime": approach to criminal justice (rehabilitation vs punishment)
- "housing": view on housing policy
- "economy": view on government economic intervention
- "media": trust in mainstream media

Also include:
- "opinion_drift": [{{topic, original_position, current_position, trigger, year}}] (2-3 topics where views changed)
- "profile_summary": one-paragraph DYNAMICS-8 personality summary"""


# ---------------------------------------------------------------------------
# Build pipeline
# ---------------------------------------------------------------------------

async def _generate_one_persona(skeleton, api_key, semaphore, persona_guidance=None):
    """Run 3-pass generation for a single persona."""
    async with semaphore:
        pid = skeleton["persona_id"]

        # Pass 1: biography.
        raw1 = await call_ollama(_pass1_prompt(skeleton, persona_guidance=persona_guidance),
                                 api_key=api_key)
        data1 = parse_json_response(raw1)
        biography = (data1 or {}).get("biography", raw1)

        # Pass 2: structured fields.
        raw2 = await call_ollama(_pass2_prompt(skeleton, biography), api_key=api_key,
                                 max_output_tokens=6144)
        structured = parse_json_response(raw2) or {}

        # Pass 3: questionnaire.
        raw3 = await call_ollama(_pass3_prompt(skeleton, biography, structured), api_key=api_key)
        questionnaire_data = parse_json_response(raw3) or {}

        # Merge everything.
        persona = {
            "persona_id": pid,
            "biography": biography,
            "identity": structured.get("identity", skeleton["identity"]),
            "dynamics_8": skeleton["dynamics_8"],
            "political": structured.get("political", {}),
            "financial": structured.get("financial", {}),
            "beliefs": structured.get("beliefs", {}),
            "emotional_state": structured.get("emotional_state", {}),
            "relationships": structured.get("relationships", []),
            "lifecycle": structured.get("lifecycle", {}),
            "religious_cultural": structured.get("religious_cultural", {}),
            "memory": structured.get("memory", {"episodic": []}),
            "questionnaire": questionnaire_data.get("questionnaire", {}),
            "profile_summary": questionnaire_data.get("profile_summary", ""),
            "opinion_drift": questionnaire_data.get("opinion_drift", []),
        }

        # Ensure identity has name fields.
        if "first_name" not in persona["identity"]:
            persona["identity"]["first_name"] = skeleton["identity"].get("first_name", "")
        if "surname" not in persona["identity"]:
            persona["identity"]["surname"] = skeleton["identity"].get("surname", "")

        return persona


async def run_build_pipeline(job_id, skeletons, api_key, db_pool, panel_name,
                              panel_description, country, simulation_depth="off",
                              persona_guidance=None):
    """Run the full generation pipeline for a build job."""
    total = len(skeletons)
    semaphore = asyncio.Semaphore(BUILD_CONCURRENCY)
    generated = []

    # Update DB status.
    conn = db_pool.getconn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE panel_build_jobs SET status='generating', progress_total=%s WHERE id=%s",
                    (total, job_id))
        conn.commit()
        cur.close()
    finally:
        db_pool.putconn(conn)

    # Generate personas in parallel batches.
    completed = 0
    batch_size = BUILD_CONCURRENCY * 2

    for batch_start in range(0, total, batch_size):
        batch = skeletons[batch_start:batch_start + batch_size]
        pass_num = 1 + (batch_start * 3 // total)  # approximate pass number
        _update_progress(job_id, completed, total, f"pass_{min(pass_num, 3)}")

        tasks = [_generate_one_persona(s, api_key, semaphore,
                                       persona_guidance=persona_guidance) for s in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            completed += 1
            if isinstance(result, Exception):
                log.error("Build job %s: persona generation failed: %s", job_id, result)
            else:
                generated.append(result)

            _update_progress(job_id, completed, total, f"generating")

            # Update DB progress periodically.
            if completed % 10 == 0:
                conn = db_pool.getconn()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE panel_build_jobs SET progress_current=%s WHERE id=%s",
                        (completed, job_id))
                    conn.commit()
                    cur.close()
                finally:
                    db_pool.putconn(conn)

    # Import generated personas into soul_personas and create panel.
    _update_progress(job_id, completed, total, "importing")
    conn = db_pool.getconn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE panel_build_jobs SET status='importing', progress_current=%s WHERE id=%s",
                    (completed, job_id))
        conn.commit()

        persona_uuids = []
        namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

        for persona in generated:
            pid = persona["persona_id"]
            persona_uuid = uuid.uuid5(namespace, f"kronaxis-{pid}")
            persona_uuids.append(persona_uuid)

            identity = persona.get("identity", {})
            name = f"{identity.get('first_name', '')} {identity.get('surname', '')}".strip()
            if not name:
                name = pid

            dynamics = persona.get("dynamics_8", {})
            life_narrative = json.dumps(persona, default=str)

            cur.execute("""
                INSERT INTO soul_personas (id, name, age, occupation, location, dynamics,
                                           life_narrative, mode, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'synthetic', 'ready')
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name, age = EXCLUDED.age,
                    occupation = EXCLUDED.occupation, location = EXCLUDED.location,
                    dynamics = EXCLUDED.dynamics, life_narrative = EXCLUDED.life_narrative
            """, (
                persona_uuid, name, identity.get("age"),
                identity.get("occupation", ""),
                f"{identity.get('region', '')}, {identity.get('town', '')}",
                json.dumps(dynamics),
                life_narrative,
            ))

            # Seed biographical memories.
            bio = persona.get("biography", "")
            if bio:
                mem_id = uuid.uuid4()
                cur.execute("""
                    INSERT INTO soul_memory (id, persona_id, content, entry_type, source, importance)
                    VALUES (%s, %s, %s, 'lived', 'panel_builder', 0.7)
                    ON CONFLICT DO NOTHING
                """, (mem_id, persona_uuid, bio[:2000]))

            # Seed episodic memories.
            episodic = persona.get("memory", {}).get("episodic", [])
            for ep in episodic[:4]:
                ep_id = uuid.uuid4()
                cur.execute("""
                    INSERT INTO soul_memory (id, persona_id, content, entry_type, source, importance)
                    VALUES (%s, %s, %s, 'lived', 'panel_builder', 0.6)
                    ON CONFLICT DO NOTHING
                """, (ep_id, persona_uuid, str(ep)[:2000]))

        # Create the panel.
        panel_id = uuid.uuid4()
        cur.execute("""
            INSERT INTO soul_panels (id, name, description, persona_ids, status, spec, simulation_depth)
            VALUES (%s, %s, %s, %s, 'ready', %s, %s)
        """, (
            panel_id, panel_name, panel_description or f"{country} panel ({len(persona_uuids)} personas)",
            persona_uuids, json.dumps({"country": country, "generated": True}),
            simulation_depth,
        ))

        # Mark job complete.
        cur.execute("""
            UPDATE panel_build_jobs
            SET status='complete', panel_id=%s, progress_current=%s,
                completed_at=NOW()
            WHERE id=%s
        """, (panel_id, len(generated), job_id))

        conn.commit()
        cur.close()
        log.info("Build job %s complete: %d personas, panel %s", job_id, len(generated), panel_id)
    except Exception as e:
        conn.rollback()
        log.error("Build job %s import failed: %s", job_id, e)
        cur = conn.cursor()
        cur.execute("UPDATE panel_build_jobs SET status='failed', error_message=%s WHERE id=%s",
                    (str(e)[:1000], job_id))
        conn.commit()
        cur.close()
    finally:
        db_pool.putconn(conn)

    _clear_progress(job_id)


def start_build_job(job_id, name, country, target_count, demographic_spec, db_pool,
                    api_key=None, simulation_depth="off", persona_guidance=None):
    """Launch a build job in a background thread."""
    key = api_key  # Retained for interface compatibility; unused by Ollama

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Generate demographic profile.
            _update_progress(job_id, 0, target_count, "profiling")
            profile = loop.run_until_complete(
                generate_demographic_profile(country, target_count, demographic_spec)
            )

            # Generate skeletons.
            _update_progress(job_id, 0, target_count, "sampling")
            skeletons = generate_skeletons(profile, target_count, spec=demographic_spec)

            conn = db_pool.getconn()
            try:
                cur = conn.cursor()
                cur.execute("UPDATE panel_build_jobs SET status='sampling', progress_total=%s WHERE id=%s",
                            (target_count, job_id))
                conn.commit()
                cur.close()
            finally:
                db_pool.putconn(conn)

            # Run the 3-pass pipeline.
            loop.run_until_complete(
                run_build_pipeline(job_id, skeletons, key, db_pool, name,
                                   f"Generated {country} panel", country,
                                   simulation_depth=simulation_depth,
                                   persona_guidance=persona_guidance)
            )
        except Exception as e:
            log.error("Build job %s failed: %s", job_id, e)
            conn = db_pool.getconn()
            try:
                cur = conn.cursor()
                cur.execute("UPDATE panel_build_jobs SET status='failed', error_message=%s WHERE id=%s",
                            (str(e)[:1000], job_id))
                conn.commit()
                cur.close()
            finally:
                db_pool.putconn(conn)
            _clear_progress(job_id)
        finally:
            loop.close()

    thread = threading.Thread(target=_run, daemon=True, name=f"build-{job_id}")
    thread.start()
    return thread
