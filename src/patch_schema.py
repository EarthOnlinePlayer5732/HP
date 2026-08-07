"""Block-table helpers used by the HybridPatch executor."""

from splitters import split_struct2


def block_id_for(filename, seq):
    """Return a stable id for a file and its sequential block index."""
    return f"{filename}:{seq}"


def build_block_table(context, splitter_fn=split_struct2):
    """Build content bytes and ordered blocks for every file in a context."""
    block_table = {}
    file_blocks = {}
    for filename, content in context.items():
        blocks = splitter_fn(content.encode("utf-8"))
        file_blocks[filename] = blocks
        for block in blocks:
            block_table[block_id_for(filename, block.block_id)] = block.data
    return block_table, file_blocks
