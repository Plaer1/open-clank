# Make a complete tiny round

- Replace the movement script with the original complete version below. Collect three squares before the timer expires. Circle-square artwork is symbolic: collection deliberately uses center distance ≤24, not a polygon collision solver.
- The `collected` signal separates detection from score handling. Reset restores every round variable and the timer; it does not add another signal connection.

```gdscript
extends Node2D
signal collected
var player := Vector2(80, 180)
var targets := [Vector2(260, 180), Vector2(460, 100), Vector2(460, 280)]
var score := 0
var running := false

func _ready():
    collected.connect(_on_collected)
    $Restart.pressed.connect(reset_round)
    $RoundTimer.timeout.connect(_on_timeout)
    reset_round()

func reset_round():
    player = Vector2(80, 180)
    score = 0
    running = true
    $Status.text = "0 / 3 — arrows move; R restarts"
    $RoundTimer.start(20.0)
    queue_redraw()

func _physics_process(delta):
    if Input.is_action_just_pressed("restart"):
        reset_round()
    if running:
        player += Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down") * 180.0 * delta
        player.x = clampf(player.x, 12.0, 628.0)
        player.y = clampf(player.y, 60.0, 348.0)
        if player.distance_to(targets[score]) <= 24.0:
            collected.emit()
    queue_redraw()

func _on_collected():
    score += 1
    $Status.text = "%d / 3" % score
    if score == targets.size():
        running = false
        $RoundTimer.stop()
        $Status.text = "3 / 3 — complete; R restarts"

func _on_timeout():
    running = false
    $Status.text = "%d / 3 — time ended; R restarts" % score

func _draw():
    draw_circle(player, 12.0, Color(0.2, 0.7, 1.0))
    if running:
        draw_rect(Rect2(targets[score] - Vector2(10, 10), Vector2(20, 20)), Color(1.0, 0.8, 0.2))
```

- The player and target use different shapes as well as colors. The timer is a design choice; an untimed assist can stop it after reset.

**Self-check:** Run a win, a timeout and a restart after each. Change one rule, predict its effect and record the result. Submit the script and reproduction notes.
