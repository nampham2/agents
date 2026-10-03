# Hand over a verifier

Read this only when the handoff gives the successor a script, check or observation window to run;
[handoff-writing.md](handoff-writing.md) covers the rest of a handoff.

When handing over a verifier, state its command, intended exit meanings (including pending) and
observed test status, and link evidence for the script version, inputs, environment, exercised
branches and results. A pending run does not exercise the completed success or failure branch.
Prefer safe fixtures, a controlled clock or shortened input to exercise terminal branches, and say
what that test establishes: a shortened test neither satisfies the real observation window nor
authorizes the gated action.

For an unexercised path say, for example: `UNVERIFIED: exit-0 terminal path unexercised; only
pending exit 3 observed.` Explain how to tell a subject failure from a checker fault: a traceback,
missing dependency or malformed output is a checker fault with no subject verdict, and exit 1 alone
may not distinguish them. On an ambiguous failure inspect the retained output and the verifier
before diagnosing the subject or restarting an observation window. Keep gated actions blocked until
a valid passing verdict and the required authorization both exist.
