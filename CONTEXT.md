# Personal finances

An aggregate view of personal finances, independent of where financial records come from.

## Language

**Source**: A connection or statement feed that provides balances, spending, holdings, or some combination of them.
_Avoid_: Institution, Item (when referring to the feed)

**Institution**: The bank, broker, or wallet associated with a source. Multiple sources may belong to one institution.

**Account**: A distinct financial account or statement wallet whose identity remains useful as connections change.
_Avoid_: Provider account identifier

**Data kind**: Balances, spending, or holdings that a source or account can provide. Availability is different from a zero balance or an empty period.
_Avoid_: Product

**Category**: A financial purpose in the shared money taxonomy; detailed categories refine that purpose across sources.

**Peer payment**: Money sent to or received from another person, tracked separately from bank transfers and ordinary purchases.

**Freshness**: How recently a source successfully supplied data, together with whether it is currently healthy. A recent sync does not guarantee institution data is complete or current.

**Historical account**: An account no longer reported as available whose past financial activity remains useful.
