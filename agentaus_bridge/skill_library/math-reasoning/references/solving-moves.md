# Solving moves when the direct method stalls

Adapted from the math-olympiad solver heuristics (Pólya plus competition practice). Try these
when no formula fits. Each move ends in something you can compute in the one batch script.

## Moves

1. **Specialise.** Solve n = 2, 3, 4, 5 by brute force in Python. The pattern is often the answer. Test past the first non-trivial case - the smallest cases may be degenerate.
2. **Have you seen a related problem?** Look for one with the same unknown or the same structure, not the same wording.
3. **Work backwards.** Start from what is asked. What would give it? What would give that? Stop when you reach something you know.
4. **Drop a condition.** Relax one constraint and see where the result breaks. That point shows what the condition is for.
5. **Add an auxiliary variable.** Name an intermediate quantity (a rate, a total, a complement) and solve for it first.
6. **Use the complement.** "At least one" is usually 1 minus "none".
7. **Find an invariant.** In a process (a game, repeated transformation, iteration), look for a quantity that never changes: a sum, a parity, a value modulo something.
8. **Take the extreme.** Look at the largest, smallest or first object; it often has properties the others lack.
9. **Count twice.** Count the same set two ways (by rows and by columns, by pairs and by items) and equate.
10. **Use symmetry, carefully.** If the problem is symmetric in x, y, z you may assume x ≤ y ≤ z - but only if the question asked is symmetric too.
11. **Go to coordinates.** For geometry, place points on axes to remove freedom (one at the origin, one on the x-axis) and compute numerically.
12. **Break the symmetry.** If the answer involves √n or log n, the best structure is often not the obvious symmetric one. Brute-force n = 3..8 before theorising.

## Growth trap

A recurrence like `b(n+1) = P(b(n))` with P of degree 2 or more grows doubly exponentially:
`b(30)` can have more digits than fit in memory. Do not compute it. Work modulo m (or track
only the value you need, such as its last digits or its parity) from the start.

## Look back (after you have an answer)

- **Can you check it?** Plug in a small case. Does n = 3 give what the formula says?
- **Can you get it differently?** A second method is a verification, and often shorter.
- **Is the bound tight?** If you proved "at most N", find the case that reaches N. If you cannot, the bound may be loose.
- **What did you use?** If some condition was never used, either the result is stronger than asked or a step is wrong.
