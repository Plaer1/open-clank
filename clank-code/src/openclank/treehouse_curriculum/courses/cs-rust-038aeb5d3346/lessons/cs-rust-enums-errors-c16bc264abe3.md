# Represent absence and failure

- An enum represents alternatives. Option<T> is Some(value) or None; Result<T,E> is Ok(value) or Err(error).
- `checked_add` returns None when bounded addition would overflow.
- Handle expected absence/failure explicitly. `unwrap` turns an unhandled case into a panic.

**Try:** For `x.checked_add(1)` on u32, state results for2 and u32::MAX. Sketch how to display either a value or an overflow message.
