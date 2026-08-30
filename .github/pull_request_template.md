<!--
Keep this short. What changed, and why the change is right — not a restatement
of the diff, which the reviewer can read.
-->

## What this changes



## Why

<!-- If it fixes something, say what the failure looked like. A bug's *shape*
     is more useful six months later than its patch. -->

---

## The gate

- [ ] `make gate` passes (coverage + tests + lint + security)
- [ ] Requirement or issue IDs cited in the commit message and test names
- [ ] Any test I removed no longer described real behaviour — named here, with what replaced it
- [ ] `docs/implementation-plan.md` §2.5 walked by hand for every area touched
- [ ] I have **seen it working**, not only seen the tests pass

<!--
That last one is not a formality. Five of this project's worst bugs survived a
fully green suite: a module system the daemon never constructed, a wire shape
every test agreed with and the daemon did not, a published module API that could
not be implemented as written, query-string secrets written to disk unredacted,
and the default exclusion list — which six tests asserted was present, and which
was never actually in the repository. docs/sprint-log.md has the details.

Please do not squash. One branch per piece of work, merged --no-ff; granular
history is a deliberate requirement here.
-->
