"""A realistic raw GraphQL user node, used to unit-test signal derivation."""

from __future__ import annotations

ML_ENGINEER_NODE = {
    "name": "Ada Researcher",
    "login": "ada",
    "email": "ada@stripe.com",
    "company": "@stripe",
    "location": "San Francisco, CA",
    "bio": "ML engineer. I train big models.",
    "websiteUrl": "https://ada.dev",
    "twitterUsername": "ada",
    "createdAt": "2013-04-01T00:00:00Z",
    "followers": {"totalCount": 1200},
    "following": {"totalCount": 50},
    "repositories": {
        "nodes": [
            {
                "name": "fast-transformer",
                "description": "A fast PyTorch transformer with CUDA kernels.",
                "stargazerCount": 2100,
                "forkCount": 300,
                "pushedAt": "2025-06-01T00:00:00Z",
                "createdAt": "2019-01-01T00:00:00Z",
                "isArchived": False,
                "primaryLanguage": {"name": "Python"},
                "repositoryTopics": {
                    "nodes": [
                        {"topic": {"name": "pytorch"}},
                        {"topic": {"name": "transformers"}},
                        {"topic": {"name": "cuda"}},
                    ]
                },
                "languages": {
                    "edges": [
                        {"size": 90000, "node": {"name": "Python"}},
                        {"size": 10000, "node": {"name": "Cuda"}},
                    ]
                },
            },
            {
                "name": "notes",
                "description": "Personal notes",
                "stargazerCount": 3,
                "forkCount": 0,
                "pushedAt": "2021-01-01T00:00:00Z",
                "createdAt": "2020-01-01T00:00:00Z",
                "isArchived": True,
                "primaryLanguage": {"name": "Python"},
                "repositoryTopics": {"nodes": []},
                "languages": {"edges": [{"size": 5000, "node": {"name": "Python"}}]},
            },
        ]
    },
    "organizations": {"nodes": [{"login": "stripe", "name": "Stripe"}]},
    "contributionsCollection": {
        "totalCommitContributions": 850,
        "totalPullRequestContributions": 120,
        "restrictedContributionsCount": 40,
    },
}

# A frontend dev whose JS fraction clears the 0.2 gate; and a Python dev who only
# mentions "react" in a description but has no JS (must NOT get the frontend marker).
FRONTEND_NODE = {
    "login": "fe",
    "createdAt": "2016-01-01T00:00:00Z",
    "repositories": {
        "nodes": [
            {
                "name": "cool-ui",
                "description": "A React + Vite component library with Tailwind.",
                "stargazerCount": 40,
                "pushedAt": "2025-01-01T00:00:00Z",
                "createdAt": "2020-01-01T00:00:00Z",
                "isArchived": False,
                "primaryLanguage": {"name": "TypeScript"},
                "repositoryTopics": {"nodes": [{"topic": {"name": "react"}}]},
                "languages": {
                    "edges": [
                        {"size": 80000, "node": {"name": "TypeScript"}},
                        {"size": 20000, "node": {"name": "CSS"}},
                    ]
                },
            }
        ]
    },
}
