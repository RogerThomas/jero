# A note on AI usage

jero's design and architecture are entirely the author's — motivated by years of
building on Django/DRF, Flask, Express, and FastAPI, and re-implementing the same
patterns jero now formalises. The implementation and documentation (with the exception
of this note), however, were heavily assisted by AI (OpenAI Codex).

The approach has been an informal spec-driven loop: a spec, usually with a small
snippet of the desired user-facing interface, which the agent builds under a short
human/agent feedback cycle. Tests carry extra weight in this model, so jero's suite is
purposefully written to target only the
[public, user-facing interface](guide/testing-approach.md): when the comprehensive
suite passes, the user-facing behaviour is correct, and the AI is allowed freedom to
refactor within those confines.

The author has endeavoured to proofread all the documentation and most of the
generated code; no doubt some pieces remain that he himself might raise an eyebrow at.
That, in the author's opinion, is one of the tradeoffs of fully embracing AI-assisted
development.

If you disagree or have concerns, the author would love to hear your thoughts —
please reach out on
[GitHub Discussions](https://github.com/RogerThomas/jero/discussions).
