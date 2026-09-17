# Wiki template

Copy this directory to become the knowledge base Logbook writes into. The
wiki is its own git repository, kept beside the machinery:

```sh
cp -r wiki-template wiki
git -C wiki init && git -C wiki add -A && git -C wiki commit -m "empty wiki"
```

`AGENTS.md` is the contract every agent writing into the wiki must follow
(page types, frontmatter, provenance rules); `CLAUDE.md` points Claude at it.
The empty directories are the page trees the orchestrator expects. `dbwiki
health` reports `wiki_missing` until `wiki/` exists and is a git checkout.
