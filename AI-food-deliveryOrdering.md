```
# AI Food Delivery Ordering Browser Agent --- Build Context

## Objective

Build a browser automation agent that searches a real food delivery platform (Uber Eats first) and returns the **best possible meal cart** that satisfies strict nutritional and budget constraints.

This agent does **NOT** complete checkout.
It prepares and evaluates options, then returns links for manual checkout.

The goal is to feel magical, fast, and consumer-grade.

---

## Core User Request (Hard Constraints)

The agent must find a **meal cart** that satisfies **ALL** of the following:

- 📍 Delivery Address:
  - **3333 S La Cienaga Blvd, Los Angeles, CA 90016**
- 💪 Protein:
  - **At least 100g total protein**
  - Can be from one item or multiple items in a cart
- 💰 Budget:
  - **Under $30 total (before tax + tip)**
- 🚚 Platform:
  - **Uber Eats (v1)**
- ⏱ Delivery:
  - Prefer faster delivery when ranking, but not a hard constraint

---

## What the Agent Should Do (High Level)

1. Open Uber Eats
2. Set delivery location to the provided address
3. Search for high-protein-friendly restaurants
   - Examples: chicken bowls, grilled chicken, burrito bowls, Mediterranean, burgers, protein bowls
4. Open multiple restaurants (3--5)
5. Navigate menus
6. Extract meals and metadata:
   - Item name
   - Price
   - Protein (if visible)
   - Calories (if visible)
   - Delivery ETA
   - Item URL
7. Combine items into a **single cart** if needed to reach 100g protein
8. Ensure total cart price is under $30
9. Rank options
10. Return the **top 3 cart options** as structured JSON

---

## Key Constraints & Rules

- ❌ Do NOT auto-purchase
- ❌ Do NOT sign into user accounts
- ❌ Do NOT store payment data
- ❌ Do NOT fabricate nutrition values
  - If protein/calories are not visible, mark as `null`
- ✅ Use only information visible on the website
- ✅ Prefer meals with explicitly listed protein

---

## Ranking Logic (Priority Order)

When comparing valid carts:

1. Meets protein target (>= 100g)
2. Under $30 total
3. Higher protein per dollar
4. Faster delivery ETA
5. Fewer items (simpler cart preferred)

---

## Expected Output Format (Strict JSON)

Return **only valid JSON**, no commentary.

```json
{
  "location": "3333 S La Cienaga Blvd, Los Angeles, CA 90016",
  "constraints": {
    "min_protein_grams": 100,
    "max_price_usd": 30
  },
  "results": [
    {
      "restaurant": "Sweetgreen",
      "cart_items": [
        {
          "item_name": "Chicken Pesto Bowl",
          "price": 17.45,
          "protein_grams": 45,
          "calories": 620,
          "url": "https://www.ubereats.com/..."
        },
        {
          "item_name": "Extra Grilled Chicken",
          "price": 6.50,
          "protein_grams": 25,
          "calories": 180,
          "url": "https://www.ubereats.com/..."
        }
      ],
      "total_price": 23.95,
      "total_protein_grams": 110,
      "eta_minutes": 32,
      "reason": "Highest protein-per-dollar combo under $30 with fast delivery"
    }
  ]
}
```

If fewer than 3 valid carts exist, return as many as possible.

* * * * *

**Failure Handling**
--------------------

If constraints cannot be met:

-   Return an empty results array

-   Include a failure_reason field explaining why (e.g. protein not visible, prices too high)

Example:

```
{
  "results": [],
  "failure_reason": "No visible meals could reach 100g protein under $30 at this location"
}
```

* * * * *

**Important Notes for the Agent**
---------------------------------

-   This is a **real browser automation task**, not a hypothetical search

-   Selector robustness matters more than speed

-   The output JSON is consumed directly by a mobile UI

-   Accuracy > completeness

-   Do not hallucinate missing data

* * * * *

**Definition of Success**
-------------------------

-   One prompt → one agent run → real Uber Eats links

-   Cart meets protein + price constraints

-   Output is deterministic and parseable

-   Feels like an AI assistant doing annoying work for the user

This agent is the core engine of a consumer mobile app.

Build for reliability and clarity over cleverness.

```
---
