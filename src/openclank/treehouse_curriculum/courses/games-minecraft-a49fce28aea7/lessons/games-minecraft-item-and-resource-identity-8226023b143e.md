# Give an item one identity

- Add these complete classes beneath `src/main/java/org/example/targetsquare/`. The initializer triggers static item registration. No creative-tab insertion is included; use `/give` to inspect it.

`TargetSquare.java`
```java
package org.example.targetsquare;

import net.fabricmc.api.ModInitializer;

public final class TargetSquare implements ModInitializer {
    public static final String MOD_ID = "target_square";

    @Override
    public void onInitialize() {
        MarkerItems.initialize();
    }
}
```

`MarkerItems.java`
```java
package org.example.targetsquare;

import net.minecraft.core.Registry;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.core.registries.Registries;
import net.minecraft.resources.Identifier;
import net.minecraft.resources.ResourceKey;
import net.minecraft.world.item.Item;

public final class MarkerItems {
    private static final ResourceKey<Item> MARKER_KEY = ResourceKey.create(
        Registries.ITEM,
        Identifier.fromNamespaceAndPath(TargetSquare.MOD_ID, "marker")
    );
    public static final Item MARKER = Registry.register(
        BuiltInRegistries.ITEM,
        MARKER_KEY,
        new Item(new Item.Properties().setId(MARKER_KEY))
    );

    private MarkerItems() { }
    public static void initialize() { }
}
```

`src/main/resources/fabric.mod.json`
```json
{
  "schemaVersion": 1,
  "id": "target_square",
  "version": "${version}",
  "name": "Target Square",
  "description": "An original marker item and one-stick practice recipe.",
  "environment": "*",
  "entrypoints": {
    "main": [
      "org.example.targetsquare.TargetSquare"
    ]
  },
  "depends": {
    "fabricloader": ">=0.19.5",
    "minecraft": "1.21.11",
    "java": ">=21",
    "fabric-api": "*"
  }
}
```

- Runtime dependency ranges do not replace the exact lab versions. Keep pinned Loader/API when testing.
- Beneath `src/main/resources/assets/target_square/`, add:

`lang/en_us.json`
```json
{"item.target_square.marker":"Target Marker"}
```

`models/item/marker.json`
```json
{"parent":"minecraft:item/generated","textures":{"layer0":"target_square:item/marker"}}
```

`items/marker.json`
```json
{"model":{"type":"minecraft:model","model":"target_square:item/marker"}}
```

- Paint `textures/item/marker.png`:16×16 RGBA; transparent outside x/y2–13; cyan(0,190,220,255) inside; perimeter x2/13 or y2/13 dark(20,30,40,255); center x/y6–9 white. The extension must represent actual PNG encoding.
- Build/run; `/give @s target_square:marker 1` should show Target Marker and that image. In a copy, change model reference to `target_square:item/makrer`, rebuild/relaunch, observe the client-resource failure, then restore. Java registration and appearance are separate boundaries.

**Self-check:** Show the item in the development client and submit the registration code plus a resource-path list. Diagnose one deliberately misspelled model reference, then restore it.
