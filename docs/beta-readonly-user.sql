-- Read-only Postgres user for the beta Railway service.
-- Run this in the Railway Postgres console (Query tab) once.
-- Replace 'CHOOSE_A_STRONG_PASSWORD' with a real password before running.

CREATE USER wb_beta WITH PASSWORD 'CHOOSE_A_STRONG_PASSWORD';

-- Allow connection to the database
GRANT CONNECT ON DATABASE railway TO wb_beta;

-- Allow usage of the public schema
GRANT USAGE ON SCHEMA public TO wb_beta;

-- Read-only access to all current tables
GRANT SELECT ON ALL TABLES IN SCHEMA public TO wb_beta;

-- Read-only access to any tables created in the future
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO wb_beta;
