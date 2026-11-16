"""The optional AI assistant: explain a finding, draft a fix. Never decides, never executes.

    evidence.py  the evidence bundle: built by code from a finding, literals redacted, with
                 an id per item that the model must cite
    provider.py  the provider interface, OpenAI, and a fake provider for tests
    answers.py   prompts, output schemas, citation checks, forbidden actions, the cache
    router.py    settings and the explain / draft endpoints

Rules decide what's wrong (ADR 0008); the model only explains it in plain language and drafts
SQL or a mongosh command for a person to review (ADR 0011).
"""
