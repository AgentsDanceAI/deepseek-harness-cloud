# DSH Cloud WeChat Mini Program

English | [简体中文](README.zh-CN.md)

This directory is a native WeChat Mini Program scaffold with no build step. The
home and about pages are native; the workspace is a `web-view` that loads the DSH
Cloud web application.

## Production prerequisites

1. Register and verify an enterprise Mini Program account.
2. Replace the placeholder AppID in `project.config.json`.
3. Use an ICP-filed mainland domain owned by the same entity and configure it as
   a WeChat business domain.
4. Host WeChat's ownership-verification file at the domain root.
5. Replace the current workspace URL in the index and webview page scripts.
6. Test login, cookie persistence, failure fallback, and every review path on a
   real device.

The existing `dhc_session` cookie remains inside the embedded browser; no Mini
Program credential bridge is included. A future WeChat sign-in flow would need a
server-validated code-to-openid exchange and explicit account binding.

Before submission, choose an appropriate tools/productivity category and remove
virtual-payment prompts from the iOS Mini Program experience. The scaffold can be
previewed in WeChat Developer Tools with domain validation disabled, but it is not
production-ready until the verified business domain is configured.
