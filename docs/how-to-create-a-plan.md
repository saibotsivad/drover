# How to Create a Plan

This guide describes when to write a planning document, what to consider before proposing a plan, and what to do once a plan is adopted.

---

## When to Write a Planning Document

Not every change needs a plan in `/docs/planning`. A good candidate is a change that:

- Affects more than one service, team, or system boundary
- Requires coordinated implementation across multiple tickets or sprints
- Introduces a new pattern, dependency, or technology
- Changes behavior in a way that is difficult or costly to reverse
- Would leave a future engineer asking "why did we do it this way?"

If a change is small, self-contained, and obviously correct, just do it and record the outcome in an ADR if it establishes a lasting decision. Planning documents are for things that need buy-in and coordination before work starts.

---

## Before Writing a Plan

Do the homework first. A plan that hasn't considered existing context will waste reviewers' time and likely come back for revision.

**Understand the current state**

- Read relevant existing ADRs in `/docs/decisions` to understand prior decisions that bear on this area.
- Read relevant maintenance documentation in `/docs` to understand operational constraints.
- Talk to engineers who have worked in the affected area. Ask what has been tried before and why it was or wasn't done.

**Define the problem clearly**

- Write down the problem you are solving in one or two sentences before writing any proposed solution. If you can't state the problem clearly, the plan isn't ready.
- Distinguish between the root cause and symptoms. Proposals that address symptoms tend to create new problems.

**Consider alternatives**

- Identify at least two or three meaningfully different approaches.
- For each alternative, consider: implementation cost, operational cost, reversibility, and alignment with existing architecture.
- Understand why you are recommending one over the others. The recommendation is stronger if the tradeoffs are made explicit.

**Identify constraints and risks**

- What could go wrong during implementation?
- What are the dependencies — on other teams, systems, or in-flight work?
- What does rollback look like if the plan doesn't work out?
- Are there compliance, security, or data-sensitivity concerns?

**Check for impact on existing documentation**

- Which ADRs, if any, will this plan supersede or contradict?
- Which sections of `/docs` will need to be updated when this is implemented?
- Make a note of these in the plan so they aren't forgotten during execution.

---

## Writing the Planning Document

Place the document at `/docs/planning/<short-descriptive-name>.md`. Use a name that describes the goal, not the solution (e.g., `consolidate-background-job-infrastructure.md`, not `migrate-to-sidekiq.md`).

A planning document should contain:

- **Goal / Desired Outcome** — what success looks like, in observable terms
- **Background** — context a reviewer needs to evaluate the plan; link to relevant ADRs and docs rather than restating them
- **Proposal** — what you are recommending and why
- **Alternatives Considered** — what else was evaluated and why it was set aside
- **Key Decisions** — the significant choices embedded in the plan, each with enough rationale that an engineer can make consistent lower-level decisions while implementing
- **Open Questions** — anything not yet resolved that needs input before or during implementation
- **Implementation Notes** — enough detail that a competent engineer can break the work into tickets; does not need to be exhaustive
- **Risks and Mitigations** — what could go wrong and how you plan to handle it
- **Documentation Impact** — which existing documents will need to be created or updated

Keep the document honest about uncertainty. A plan with documented open questions is more useful than one that papers over them.

---

## After a Plan is Adopted

Once a plan has been reviewed and the team has committed to it:

**Create ADRs for significant decisions**

Each decision in the plan that establishes a lasting architectural pattern or constraint should become an ADR in `/docs/decisions`. An ADR captures the decision, the context at the time it was made, and the reasoning — so that future engineers understand not just what was decided but why.

A decision is worth an ADR if someone might reasonably question or revisit it later, or if it should constrain future choices.

**Update the planning document status**

Mark the planning document as adopted and link to any ADRs it produced. The planning document is a record of the reasoning process; the ADRs are the durable record of the outcomes.

**Update `/docs` as implementation progresses**

Don't wait until the end of implementation to update documentation. As each part of the plan is executed:

- Update maintenance documentation to reflect new operational procedures.
- Update any architecture diagrams or system descriptions that are now out of date.
- If the plan supersedes an existing ADR, update that ADR to mark it as superseded and link to the replacement.

**Close the loop on open questions**

If questions were documented in the planning document, make sure they were resolved — either in an ADR, a code comment, or an update to the plan itself. Unresolved questions are a common source of inconsistent implementation.

---

## Quick Reference Checklist

Before proposing a plan:

- [ ] Read relevant ADRs and maintenance docs
- [ ] Stated the problem clearly, separate from the solution
- [ ] Evaluated at least two to three alternatives
- [ ] Identified dependencies, risks, and rollback approach
- [ ] Noted which existing documents will need updating

After a plan is adopted:

- [ ] Created ADRs for each significant lasting decision
- [ ] Updated planning document status and linked ADRs
- [ ] Updated `/docs` as implementation completes
- [ ] Confirmed open questions were resolved and recorded
