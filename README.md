# Query Pulse Backend

This repository contains the backend for the Query Pulse product.

## Overview
Query Pulse is a database performance intelligence product focused on explaining why database workloads are slow or expensive.

The backend is responsible for:
- managing database sources
- connecting to PostgreSQL and MongoDB
- collecting workload metrics and query evidence
- storing observations and fingerprints
- detecting rule-based findings
- exposing a clean FastAPI API to the frontend

## Current status
This repository is under active development.

The current work is focused on the FastAPI-based backend foundation and the next real workload-collection phase.

## Planned architecture
- `app.py` — FastAPI application entrypoint and route registration
- `config/` — environment and runtime configuration for the FastAPI app
- `apps/` — reserved for the next domain modules and source adapters
- `apps/sources/` — database source configuration and validation
- `apps/workload/` — metrics, observations, query fingerprints and plans
- `apps/findings/` — rule engine and findings management
- `apps/ai/` — AI integrations and evidence bundles
- `adapters/postgres/` — PostgreSQL adapter logic
- `adapters/mongodb/` — MongoDB adapter logic
- `collector/` — polling and delta collection logic
- `demo/` — demo seed data and workloads
- `tests/` — automated tests
- `docs/adr/` — architecture decisions and rationale

## Local development
Use the project-level commands to bring up the local stack and run the app locally.

## Notes
This project is being built in phases. The implementation is intentionally documented in plain engineering terms so the code and decisions remain easy to understand as the system grows.

## Technology choice
The active backend stack is based on Python + FastAPI with Pydantic validation, async-ready endpoints, and session-based browser auth for the local demo flow.
