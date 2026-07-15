-- Development-only runtime identity. The bootstrap/owner role runs Alembic;
-- API and worker connect as this non-superuser so local RLS behavior matches
-- production. Migration 0045 grants existing and future application tables.
CREATE ROLE soa_app LOGIN PASSWORD 'soa_app_password' NOSUPERUSER NOBYPASSRLS;
GRANT CONNECT ON DATABASE soa TO soa_app;
