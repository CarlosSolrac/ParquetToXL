"""What a dataframe is, what it hashes to, and what a conversion does to it.

The canonical encoding, the hashers, the column and frame metadata models, and the
conversions are one versioned contract and therefore one distribution. ``metadata.column``
embeds ``HashedDataframe`` as a Pydantic field annotation at runtime, so the discriminated
union must resolve when the class is built; splitting hashing out would make combinations
installable that silently produce incompatible digests.
"""
