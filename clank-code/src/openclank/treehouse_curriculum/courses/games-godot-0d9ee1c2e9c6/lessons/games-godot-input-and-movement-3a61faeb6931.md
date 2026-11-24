# Move an original shape

- Attach this original script to the scene root. Its position is game data; `_draw` visualizes it. `queue_redraw` requests a new drawing.
- `get_vector` bounds diagonal input length; multiplying by seconds makes the speed meaningful.

```gdscript
extends Node2D
var player := Vector2(80, 180)

func _physics_process(delta):
    var direction := Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down")
    player += direction * 180.0 * delta
    player.x = clampf(player.x, 12.0, 628.0)
    player.y = clampf(player.y, 60.0, 348.0)
    queue_redraw()

func _draw():
    draw_circle(player, 12.0, Color(0.2, 0.7, 1.0))
```

- This is a custom-drawn controller with a bounded position, not a rigid-body physics simulation.

**Self-check:** Run it, test all four boundaries and diagonal movement, then change speed to 90. Report the observed change and the reason for multiplying by delta.
