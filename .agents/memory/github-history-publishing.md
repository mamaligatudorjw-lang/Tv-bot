---
name: GitHub history publishing
description: Safe fallback for publishing a local Git history through the connected GitHub integration when CLI push authentication is unavailable.
---

When CLI GitHub authentication is unavailable but the connected GitHub integration has repository write access, recreate commits through the Git Database API from the common ancestor instead of committing only selected files. Upload missing blobs, create each tree from its parent with the commit diff, create the commit with the original author/committer metadata, and verify the returned tree and commit SHA against local Git before proceeding.

**Why:** A contents/API commit of only the requested source files can move the remote branch to an incomplete repository. Per-object SHA checks make the final ref update atomic from the application's perspective and preserve the local history when the common ancestor is shared.

**How to apply:** Reuse the parent tree for commits whose tree is unchanged; otherwise GitHub rejects an empty tree payload. Increase the local child-process buffer when reading large Git blobs, and update the branch ref only after the full chain and the expected remote base ref have both been revalidated.