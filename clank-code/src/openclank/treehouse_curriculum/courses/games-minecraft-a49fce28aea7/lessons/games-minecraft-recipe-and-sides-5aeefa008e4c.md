# A recipe belongs to game rules

- First isolate recipe data: save this file as `src/main/resources/data/target_square/recipe/marker.json` (singular recipe).
```json
{"type":"minecraft:crafting_shapeless","category":"misc","ingredients":["minecraft:stick"],"result":{"id":"target_square:marker","count":1}}
```
- Build/run in survival. One stick in one slot yields one marker and consumes one stick. Cobblestone alone, stick+cobblestone in separate slots and two sticks in separate slots must fail. Two sticks stacked in one slot permit two successive crafts. Empty the grid between cases. `/give` proves registration, not crafting.
- Now preserve that checkpoint and switch to generated data. Remove the manual recipe from this staged copy so there is one authority. Append to build.gradle:
```groovy
fabricApi { configureDataGeneration { client = true; addToResources = true } }
```
- Add `"fabric-datagen":["org.example.targetsquare.datagen.MarkerDataGenerator"]` beside main in entrypoints. Add these complete files under `src/client/java/org/example/targetsquare/datagen/`:

`MarkerDataGenerator.java`
```java
package org.example.targetsquare.datagen;

import net.fabricmc.fabric.api.datagen.v1.DataGeneratorEntrypoint;
import net.fabricmc.fabric.api.datagen.v1.FabricDataGenerator;

public final class MarkerDataGenerator implements DataGeneratorEntrypoint {
    @Override
    public void onInitializeDataGenerator(FabricDataGenerator generator) {
        generator.createPack().addProvider(MarkerRecipeProvider::new);
    }
}
```

`MarkerRecipeProvider.java`
```java
package org.example.targetsquare.datagen;

import java.util.concurrent.CompletableFuture;
import net.minecraft.core.HolderLookup;
import net.minecraft.data.recipes.RecipeCategory;
import net.minecraft.data.recipes.RecipeOutput;
import net.minecraft.data.recipes.RecipeProvider;
import net.minecraft.world.item.Items;
import net.fabricmc.fabric.api.datagen.v1.FabricDataOutput;
import net.fabricmc.fabric.api.datagen.v1.provider.FabricRecipeProvider;
import org.example.targetsquare.MarkerItems;

public final class MarkerRecipeProvider extends FabricRecipeProvider {
    public MarkerRecipeProvider(FabricDataOutput output,
            CompletableFuture<HolderLookup.Provider> registries) {
        super(output, registries);
    }

    @Override
    protected RecipeProvider createRecipeProvider(HolderLookup.Provider lookup,
            RecipeOutput sink) {
        return new RecipeProvider(lookup, sink) {
            @Override
            public void buildRecipes() {
                shapeless(RecipeCategory.MISC, MarkerItems.MARKER)
                    .requires(Items.STICK)
                    .unlockedBy("has_stick", has(Items.STICK))
                    .save(output);
            }
        };
    }

    @Override
    public String getName() {
        return "Target Square marker recipe";
    }
}
```

- Run `./gradlew --no-daemon runDatagen`, then build. Inspect `src/main/generated/data/target_square/recipe/marker.json`; optional diagnostic depth: inspect the generated unlock advancement under `advancement/recipes/misc/`. inspect actual namespace/path rather than assuming output. Generated recipe must mean one stick→one marker; whitespace may differ. The provider is editable authority.
- Repeat the required intended-ingredient and unrelated-ingredient craft tests; the full matrix and fresh-world stick unlock check are optional diagnostics. `/reload` reloads active server data, not source Java, textures or unbuilt source files. A texture edit cannot grant authoritative inventory.

**Self-check:** Generate and inspect the recipe, run both crafting tests, and explain why a texture change cannot create an authoritative crafting result.
