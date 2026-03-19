-- Kronaxis Panel Studio - Database Schema
-- Copyright (c) 2026 Kronaxis Limited. All rights reserved.
-- https://kronaxis.co.uk | contact@kronaxis.co.uk

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================================
-- Persona Tables
-- ============================================================================

CREATE TABLE IF NOT EXISTS soul_personas (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    age INTEGER,
    gender TEXT,
    ethnicity TEXT,
    occupation TEXT,
    occupation_sector TEXT,
    education_level TEXT,
    annual_income INTEGER,
    location TEXT,
    region TEXT,
    dynamics JSONB NOT NULL,
    life_narrative TEXT,
    biography JSONB,
    mode TEXT DEFAULT 'autonomous',
    status TEXT DEFAULT 'active',
    media_diet JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_soul_personas_status ON soul_personas(status);
CREATE INDEX IF NOT EXISTS idx_soul_personas_region ON soul_personas(region);

CREATE TABLE IF NOT EXISTS soul_memory (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    persona_id UUID NOT NULL REFERENCES soul_personas(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    entry_type TEXT DEFAULT 'observation',
    source TEXT DEFAULT 'system',
    importance REAL DEFAULT 0.5,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_soul_memory_persona ON soul_memory(persona_id);

CREATE TABLE IF NOT EXISTS soul_relationships (
    persona_id UUID NOT NULL REFERENCES soul_personas(id) ON DELETE CASCADE,
    related_persona_id UUID NOT NULL REFERENCES soul_personas(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,
    strength REAL DEFAULT 0.5,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (persona_id, related_persona_id)
);

-- ============================================================================
-- Panel Tables
-- ============================================================================

CREATE TABLE IF NOT EXISTS soul_panels (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    description TEXT,
    persona_ids UUID[],
    spec JSONB,
    status TEXT DEFAULT 'active',
    owner_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS soul_panel_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    panel_id UUID REFERENCES soul_panels(id) ON DELETE CASCADE,
    stimulus TEXT,
    stimulus_type TEXT DEFAULT 'standard' CHECK (stimulus_type IN ('standard', 'custom')),
    raw_responses JSONB,
    aggregated_responses JSONB,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_soul_panel_runs_panel ON soul_panel_runs(panel_id);
CREATE INDEX IF NOT EXISTS idx_soul_panel_runs_status ON soul_panel_runs(status);

-- ============================================================================
-- Conversation Tables
-- ============================================================================

CREATE TABLE IF NOT EXISTS panel_conversations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    panel_id UUID REFERENCES soul_panels(id) ON DELETE CASCADE,
    title TEXT,
    description TEXT,
    turn_count INTEGER DEFAULT 0,
    total_responses INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    owner_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_panel_conversations_panel ON panel_conversations(panel_id);

CREATE TABLE IF NOT EXISTS panel_conversation_turns (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    conversation_id UUID REFERENCES panel_conversations(id) ON DELETE CASCADE,
    turn_number INTEGER NOT NULL,
    stimulus TEXT,
    stimulus_type TEXT DEFAULT 'standard',
    run_id UUID REFERENCES soul_panel_runs(id),
    response_count INTEGER DEFAULT 0,
    aggregated JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_panel_turns_conversation ON panel_conversation_turns(conversation_id);

-- ============================================================================
-- Panel Builder
-- ============================================================================

CREATE TABLE IF NOT EXISTS panel_build_jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    country TEXT DEFAULT 'United Kingdom',
    target_count INTEGER NOT NULL,
    demographic_spec JSONB,
    status TEXT DEFAULT 'pending',
    progress_current INTEGER DEFAULT 0,
    progress_total INTEGER DEFAULT 0,
    progress_pass TEXT,
    panel_id UUID REFERENCES soul_panels(id),
    error_message TEXT,
    owner_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

-- ============================================================================
-- Scheduling
-- ============================================================================

CREATE TABLE IF NOT EXISTS panel_schedules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    panel_id UUID REFERENCES soul_panels(id) ON DELETE CASCADE,
    conversation_id UUID REFERENCES panel_conversations(id),
    stimulus TEXT NOT NULL,
    stimulus_type TEXT DEFAULT 'standard',
    sample_size INTEGER,
    cron_expression TEXT,
    next_run_at TIMESTAMPTZ,
    last_run_at TIMESTAMPTZ,
    run_count INTEGER DEFAULT 0,
    max_runs INTEGER,
    status TEXT DEFAULT 'active',
    owner_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- Conjoint Analysis
-- ============================================================================

CREATE TABLE IF NOT EXISTS panel_conjoint_studies (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    panel_id UUID REFERENCES soul_panels(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    attributes JSONB,
    profiles JSONB,
    results JSONB,
    conversation_id UUID REFERENCES panel_conversations(id),
    status TEXT DEFAULT 'draft',
    owner_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- Authentication (optional, enabled via PANEL_STUDIO_AUTH=true)
-- ============================================================================

CREATE TABLE IF NOT EXISTS panel_studio_users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    role TEXT DEFAULT 'user',
    last_login_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- Focus Group Transcripts
-- ============================================================================

CREATE TABLE IF NOT EXISTS panel_focus_group_transcripts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id UUID REFERENCES soul_panel_runs(id) ON DELETE CASCADE,
    transcript TEXT,
    speaker_count INTEGER DEFAULT 0,
    word_count INTEGER DEFAULT 0,
    model_used TEXT,
    generated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_focus_group_run ON panel_focus_group_transcripts(run_id);

-- ============================================================================
-- LLM Call Log (cost tracking)
-- ============================================================================

CREATE TABLE IF NOT EXISTS llm_call_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider TEXT DEFAULT 'local',
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost NUMERIC(10,6) DEFAULT 0,
    latency_ms INTEGER,
    success BOOLEAN DEFAULT true,
    error_msg TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_call_log_created ON llm_call_log(created_at);

-- ============================================================================
-- Cost Tracking
-- ============================================================================

CREATE TABLE IF NOT EXISTS panel_gpu_usage (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider TEXT DEFAULT 'local',
    instance_id TEXT,
    gpu_type TEXT,
    hourly_rate NUMERIC(10,4) DEFAULT 0,
    stimulus_count INTEGER DEFAULT 0,
    total_cost NUMERIC(10,4) DEFAULT 0,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    stopped_at TIMESTAMPTZ
);
