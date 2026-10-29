# Shared line-rule test vectors (D-033)

Ingredient-line rules exist twice: here in the server (Python) and on the device in
ThymbleKit (Swift, used by the free tier). `ingredient_lines.json` pins what both must do.
The server is the reference; a difference between the two is a bug. Every line in the file
is synthetic, written for Thymble; never add text copied from a recipe site or cookbook.

The server runs the file in `tests/test_line_vectors.py`. ThymbleKit should load the same
file (copy it into the package's test resources, or point the test at a checkout of this
repo) and apply the comparison rules below.

## File format

UTF-8 JSON, LF line endings:

```json
{
  "version": 1,
  "description": "...",
  "cases": [
    {
      "id": "D03",
      "kind": "recipe_line",
      "tags": ["ditto", "ditto-quantity"],
      "previous": ["3 tablespoons flour"],
      "input": "2 do.",
      "expect": {
        "quantity": 2, "quantity_max": null, "unit": "tbsp", "name": "flour",
        "additional": [], "alternatives": [], "preparation": null,
        "optional": false, "ditto": true
      }
    }
  ]
}
```

| key | meaning |
|---|---|
| `version` | Format version. Bump it when a field changes meaning; readers should refuse versions they don't know. |
| `id` | Unique, stable case id. The letter groups cases (`Q` quantities, `G` ranges, `F` unicode fractions, `U` unit aliases, `W` word numbers and arithmetic, `D` ditto, `A` and-splits, `O` or/substitutes and no-split, `P` preparation, `V` voice items). |
| `kind` | `recipe_line` (a line in a recipe's ingredient list) or `voice_item` (a shopping-list entry typed or spoken, e.g. through Alexa). |
| `tags` | Free-form labels for filtering and reporting; not compared. |
| `previous` | `recipe_line` only, optional: the lines right before this one in the same list, oldest first. Parse them in order and compare only the last result (ditto lines copy from the line before them). |
| `input` | The raw line. |
| `expect` | Expected fields (below). Compare every field present; ignore fields your output has that aren't listed. |

### `recipe_line` fields

| field | type | rule |
|---|---|---|
| `quantity` | number or null | First amount on the line. Word numbers resolved ("a dozen" = 12, "half a" = 0.5, "a couple of" = 2, "a pinch" = 1). Simple arithmetic resolved ("2 cups minus 2 tablespoons" = 1.875 cup). |
| `quantity_max` | number or null | Upper end of a range ("2-3", "1 to 2", "4–6"); null otherwise. |
| `unit` | string or null | Canonical code: `cup`, `tbsp`, `tsp`, `lb`, `oz`, `g`, `kg`, `ml`, `l`, `can` (also "tin"), `clove`, `pinch`, ... (see `UNIT_ALIASES` in `src/thymble/importer/rules.py`). Capital `T` = tbsp, small `t` = tsp. |
| `name` | string | The thing you buy, compound names kept whole ("all-purpose flour"). Brand names of the same product are dropped. |
| `additional` | list of strings | Extra **separate shopping items**: "and", "&", "and/or", "EACH x and y". |
| `alternatives` | list of strings | Substitutes for the **same item**: "or", "sub", "(or X)". |
| `preparation` | string or null | What you do at home: chopped, melted, sifted, "drained and rinsed", and "packed" / "tightly packed" (a measuring instruction, not part of the product). |
| `optional` | bool | The line says "optional". |
| `ditto` | bool | The line was resolved from the line before it ("do.", "same as above", `2 do.`, `1 "`, `" nutmeg`). A ditto with a quantity keeps the previous line's unit. |

"and" vs "or" (D-032): "and" / "&" / "and/or" make separate items; "or" / "sub" / "(or X)"
make one item with substitutes; "or" between two names or brands of the same product
("Diamond Crystal or Morton kosher salt", "cornflour / cornstarch") is one item with no
substitutes; "or as needed" and "half-and-half" are never split.

### `voice_item` fields

| field | type | rule |
|---|---|---|
| `name` | string | The item with keywords, store hints and quantity removed. Thymble's own mirror decorations (" (small)", "— on Walmart order") are removed too. |
| `quantity`, `unit` | number / string or null | Leading amount ("2 lbs" = 2 `lb`, "1 gallon" = 1 `gal`, "3 cans" = 3 `can`). |
| `urgent` | bool | "urgent", "urgent:", "asap", "need", "we're out of" at the start; "urgent" / "asap" at the end (§6.4, D-029). |
| `store` | string or null | Store hint, lower-case: a known chain at the start ("costco paper towels") or after "from"/"at" at the end ("bread from aldi"). Spelling variants map to one value ("Trader Joes" = `trader joe's`). A store name alone is an item, not a hint. |
| `is_food` | bool | False for household, pharmacy, pet and electronics items. Matched by **whole word** (an optional plural "s"/"es" allowed): "dish soap" is not food, "soapberries" is. |

## Comparing

- Strings compare case-insensitively (Unicode case folding); `null` only equals `null`.
- Numbers compare with an absolute tolerance of 0.001 (so 1/3 may be written `0.3333`).
- Lists compare element by element, in order.
- An expected field the implementation doesn't produce is a failure, not a skip.

## Changing the file

Add cases rather than editing existing ones; never reuse an id. If a rule changes, change
the server first, update the vector in the same commit, and note it for the Swift side.
