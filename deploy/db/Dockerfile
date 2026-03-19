# Kronaxis Panel Studio - PostgreSQL with pgvector
# Copyright (c) 2026 Kronaxis Limited. All rights reserved.
FROM pgvector/pgvector:pg15

COPY enable_pgvector.sh /docker-entrypoint-initdb.d/01-enable_pgvector.sh
COPY schema.sql /docker-entrypoint-initdb.d/10-schema.sql

RUN chmod +x /docker-entrypoint-initdb.d/01-enable_pgvector.sh
