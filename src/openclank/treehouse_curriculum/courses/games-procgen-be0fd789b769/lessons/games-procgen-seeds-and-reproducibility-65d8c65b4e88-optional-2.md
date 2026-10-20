Use the complete Godot round. Add this code; call `reset_targets()` once inside reset_round before score=0 and queue_redraw. Never draw RNG values in _draw or per frame.

```gdscript
var target_seed := 3
func reset_targets():
    targets = [Vector2(260, 180), Vector2(460, 100), Vector2(460, 280)]
    var s := target_seed
    s = (5 * s + 1) % 16
    var x := 80 + 20 * s
    s = (5 * s + 1) % 16
    var y := 80 + 10 * s
    targets[0] = Vector2(x, y)
```

Drawing and collection already read targets[score], so share this array. Keep targets1/2 and distance≤24. Seed3 draws0,1→(80,90); seed4 draws5,10→(180,180). Full range x80…380,y80…230 fits reachable centers x12…628,y60…348. Reset uses the same seed and exactly two draws; an extra draw changes the recipe. Toy LCG, version1, fixed draw order. This adapter is Godot-only: Bevy stays(200,0); its y≤150 and tolerance20 cannot reach y180.