//! Deep recursion on a stack of any size: where a render descends (an expression, a block, a
//! macro, a value) it asks for room first, and when the thread's stack is low it continues on one
//! the heap provides. How deep a template may nest is bounded separately, by counters, so what
//! this costs in memory is bounded too.

/// What must remain of the stack for the code that follows, in bytes.
const RED_ZONE: usize = 48 * 1024;
/// The size of each stack taken from the heap when the thread's runs low.
const SEGMENT: usize = 256 * 1024;

/// Runs `work` with room to descend.
pub fn deeper<T>(work: impl FnOnce() -> T) -> T {
    stacker::maybe_grow(RED_ZONE, SEGMENT, work)
}
