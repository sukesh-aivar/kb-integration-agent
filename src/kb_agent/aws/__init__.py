"""AWS integration layer.

Every AWS-backed capability is defined as a Protocol in :mod:`interfaces` and has
two implementations: a real boto3-backed client, and an in-memory fake
(:mod:`fakes`) used by ``--mode fake`` so the whole pipeline runs offline and
deterministically. Pipeline code depends only on the protocols, never on boto3,
so the two run modes share one code path (spec: "dependency-inject AWS clients").
"""
