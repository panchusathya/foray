-- Runs once, on first initialization of the shared Postgres volume, as the
-- superuser (Juicebox's POSTGRES_USER). Creates a separate role + database for
-- Foray so it shares the container but never touches Juicebox's data.
create role foray with login password 'foray';
create database foray owner foray;

\connect foray
-- vector is not a trusted extension, so it must be created by a superuser here.
create extension if not exists vector;
grant all on schema public to foray;
