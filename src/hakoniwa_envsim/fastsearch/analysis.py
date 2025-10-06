# fastsearch/analysis.py

def analyze_tree(root):
    """BVHツリー構造を解析して、最大深さと平均リーフサイズを返す"""
    leaf_counts = []
    max_depth = 0

    def traverse(node, depth=0):
        nonlocal max_depth
        max_depth = max(max_depth, depth)

        if node.is_leaf:
            leaf_counts.append(len(node.ids))
        else:
            if node.left:
                traverse(node.left, depth + 1)
            if node.right:
                traverse(node.right, depth + 1)

    traverse(root)
    avg_leaf_size = sum(leaf_counts) / len(leaf_counts) if leaf_counts else 0
    return {"max_depth": max_depth, "avg_leaf_size": avg_leaf_size}
