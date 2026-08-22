# Self-host legal document directory

The Community image intentionally ships no AgentsDance operator terms, privacy
policy, acceptable-use policy, or refund policy. Put the self-host operator's
own reviewed Markdown documents in this directory when building a customized
image, or mount an operator-controlled directory at `/srv/legal`.

Root-level `legal/` files describe the hosted service and must never be copied
into the generic self-host runtime image.
