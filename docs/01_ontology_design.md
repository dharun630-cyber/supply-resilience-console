# Milestone 1: Ontology design

## What an ontology is (here)

An ERD describes **how data is stored**. An ontology describes **the world the business operates in**: the things that exist, how they relate, and what people may do to them. Every application built on it uses the same definitions.

| Building block | Meaning | In this project |
|---|---|---|
| Object type | A real-world thing with stable identity | `ERPSupplier`, `Company`, `Commodity`, `Country`, `ImportFlow`, `RiskEvent`, `PurchaseOrderLine`, `Alert` |
| Property | An attribute of an object | `fragility_score`, `share_of_uk_imports`, `criticality` |
| Link type | A named, directional relationship with a cardinality | `affects`, `resolves_to`, `located_in`, `imports`, … |
| Action type | A governed change to the world | `raise_alert`, `update_alert_status`, `confirm_match`, `reject_match`, `set_criticality` |

## Method: decision first

1. **Decision-maker:** Head of Supply Chain Resilience at a UK manufacturer.
2. **Decision:** "A disruption just happened. Which of my suppliers are exposed, how badly, and what do we do this week?"
3. **Questions:** Which countries are affected? Which commodities does the UK source from them? Which of *our* suppliers provide those? Which of those are financially fragile? What has the team already done?
4. **Nouns → objects, relationships → links, verbs → actions.** Only then pick data sources.

## Design rules applied

1. **If something has its own attributes, or other things point at it, make it an object.** That's why `Country` is an object and not a string property.
2. **Once a relationship carries data, promote it to an object.** That's why `ImportFlow` is an object between `Commodity` and `Country`.
3. **Choose primary keys for stability, not convenience.** `company_number` is canonical because names change and postcodes are shared.
4. **Resolution produces links with evidence, never destructive merges.** Keep the raw record; score the link; let humans override it.
5. **Derived properties must be explainable.** Store the components, not just the score.
6. **Some objects exist only because users act.** `Alert` has no source dataset; it's what makes the system operational rather than a report.
7. **Observed vs inferred.** Overseas vendors are exposed *directly* (`located_in`). UK vendors are exposed through an *inferred* national sourcing mix, and the model says which is which.
