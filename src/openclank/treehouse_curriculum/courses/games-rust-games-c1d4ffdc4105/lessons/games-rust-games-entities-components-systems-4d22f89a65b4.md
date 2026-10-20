# A component selects the work

- `Player` marks one entity. Its `Transform` is per-entity state. `Round` is shared round state, stored as a resource. A system declares what data it reads or mutates.
- Add this original complete example to `src/main.rs`. Arrow keys move; touching the small square wins; R resets. Score is reported in the terminal.

```rust
use bevy::prelude::*;

#[derive(Component)]
struct Player;
#[derive(Resource, Default)]
struct Round { won: bool }

fn main() {
    App::new()
        .add_plugins(DefaultPlugins)
        .init_resource::<Round>()
        .add_systems(Startup, setup)
        .add_systems(Update, play)
        .run();
}
fn setup(mut commands: Commands) {
    commands.spawn(Camera2d);
    commands.spawn((
        Sprite::from_color(Color::srgb(0.2, 0.7, 1.0), Vec2::splat(24.0)),
        Transform::default(), Player,
    ));
    commands.spawn((
        Sprite::from_color(Color::srgb(1.0, 0.8, 0.2), Vec2::splat(16.0)),
        Transform::from_xyz(200.0, 0.0, 0.0),
    ));
    println!("Score 0. Arrows move; R resets.");
}
fn play(
    keys: Res<ButtonInput<KeyCode>>, time: Res<Time>,
    mut round: ResMut<Round>,
    mut player: Query<&mut Transform, With<Player>>,
) {
    let Ok(mut t) = player.single_mut() else { return; };
    if keys.just_pressed(KeyCode::KeyR) {
        t.translation = Vec3::ZERO;
        round.won = false;
        println!("Score 0.");
    }
    if round.won { return; }
    let mut d = Vec2::ZERO;
    if keys.pressed(KeyCode::ArrowLeft) { d.x -= 1.0; }
    if keys.pressed(KeyCode::ArrowRight) { d.x += 1.0; }
    if keys.pressed(KeyCode::ArrowUp) { d.y += 1.0; }
    if keys.pressed(KeyCode::ArrowDown) { d.y -= 1.0; }
    let step = d.normalize_or_zero() * 180.0 * time.delta_secs();
    t.translation.x = (t.translation.x + step.x).clamp(-300.0, 300.0);
    t.translation.y = (t.translation.y + step.y).clamp(-150.0, 150.0);
    if (t.translation.x - 200.0).abs() <= 20.0 && t.translation.y.abs() <= 20.0 {
        round.won = true;
        println!("Score 1. Target reached; R resets.");
    }
}
```

- The 20-unit overlap threshold is the sum of half-widths: 12+8. This sample implements axis-aligned overlap itself; it does not claim a physics engine.

**Self-check:** Run it and identify the component, resource and system responsible for one win. Submit source and observed terminal output.
