# Change a heap and trace its tree

- A zero-based binary heap stores root at0; children of i are2i+1 and2i+2 when present, parent is floor((i−1)/2) for i>0.
- A min-heap has parent≤child. Insert by appending then swapping with a larger parent. Remove-min by saving root, moving last item to root, then swapping with its smaller child until order holds; choose left on an equal-child tie.
- Start `[1,4,2,8,5]`. Preorder visits root, left subtree, right subtree.

**Try:** Insert0, showing each swap. Then remove-min from that resulting heap, showing comparisons/swaps and removed value. Give the final array, its preorder and every parent-order check. Explain why neither heap array nor preorder must be sorted, and how a BST's left/right rule differs.
