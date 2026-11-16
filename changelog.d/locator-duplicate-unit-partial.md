Chapter locator: when a response puts two agenda items on one transcript unit, keep the anchors
that did not collide instead of failing the whole episode. The colliding items are left out (the
model's confidence is not used to choose between them) and recorded in the boundary artifact's
`diagnostics.unit_conflicts`. If every anchor collides, the episode gets one repair retry with a
hint naming the unit. Previously only same-start collisions were repaired, so duplicate-unit
failures were retried blind until the terminal retry gate refused the recipe.
