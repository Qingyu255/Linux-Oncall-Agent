# Filesystem capacity diagnosis

1. Identify the approved mount and report both available blocks and available inodes.
2. Use space available to the service user and record filesystem type and read-only state.
3. Capacity pressure alone does not prove an application write failed; require controlled or service
   error evidence.
4. Keep root and the dedicated lab mount separate. Never request arbitrary paths or mutate through a
   diagnostic capability.
