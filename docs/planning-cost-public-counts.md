# Public planning-cost counts (#327)

The cycles feed derives planning iterations and session counts from public-safe `planning_session` ledger fields, and planner calls from per-cycle `llm_calls` component counts. It publishes counts only; planner text is not rendered or added to the public ledger projection.

`iterations_used` remains null-safe: if any session lacks a valid non-negative integer, the aggregate is rendered as unknown rather than zero. No sessions is not enough evidence that a cycle predates the planner, so cycles without planning evidence render planning as unknown until a trustworthy historical provenance discriminator exists. The #1903 sample established zero sessions for its measured base cohort, but does not define a general cutoff for all older cycles.
