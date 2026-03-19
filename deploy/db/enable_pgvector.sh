#!/bin/bash
# Enable pgvector extension on first boot.
# Kronaxis Panel Studio | https://kronaxis.co.uk
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS vector;
EOSQL
