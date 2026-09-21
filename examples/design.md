# Designer contract

Goal: create greeting.txt containing exactly `hello triad` followed by a newline.

Constraints: preserve all other files; use the existing workspace; do not add dependencies.

Acceptance: the named greeting check passes against the final files.

Supervisor: assign the task to Worker, review its evidence, and accept only after it is ready.
Worker: implement, run the check, fix any failure, then report the run ID.
