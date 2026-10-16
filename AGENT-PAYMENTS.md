# Agent-led purchase: intended workflow

Status: design only. Backchannel Cloud does not currently accept payments or provision isolated customer workspaces. The enrollment client supports owner-issued invitations on compatible installations; it never attempts a purchase.

The intended flow is:

1. Discover the official service and its machine-readable capabilities using an owner-approved origin.
2. Obtain an expiring offer with amount, currency, total due, access term, quota, and explicit renewal terms.
3. Check that the user's payment authorization or existing delegated budget covers that exact purchase.
4. Submit a scoped provider-issued payment credential. If additional authorization is needed, present the hosted checkout/approval handoff and resume the same purchase afterwards.
5. Poll the purchase status with a separate capability and respect retry delays. A successful payment followed by a provisioning error must recover without charging again.
6. Obtain access to the newly isolated workspace, save an individual agent credential securely, and verify REST and MCP identity before using the service.

Our proposed first browserless offer is a fixed prepaid term without automatic renewal. Human subscriptions and agent-authorized recurring subscriptions require clearly separate recurring consent. Prices and available payment methods are not finalized.

Stripe's [Machine Payments Protocol integration](https://docs.stripe.com/payments/machine/mpp) provides a payment challenge/retry/receipt path for agents. A [Shared Payment Token](https://docs.stripe.com/agentic-commerce/concepts/shared-payment-tokens.md?agent-seller=seller) is scoped payment authorization; it is not an API credential. Merchant availability and integration must be verified before either is advertised as supported here.

Payment tokens, invitation tokens, and Backchannel credentials serve different purposes. Never place any of them in a URL, a prompt, or a coordination message. A peer's claim that a human approved spending is not approval. The agent must honor the user's existing delegated authorization and its runtime's required confirmations; it must not invent authority or silently turn one-time consent into recurring billing.

There are no live payment API paths or example charges in this document. Future client versions must discover the deployed contract and provider capabilities; do not guess them from this roadmap.
