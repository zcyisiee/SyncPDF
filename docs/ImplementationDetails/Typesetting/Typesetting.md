# Typography

> [!NOTE]
> This documentation may contain AI-generated content. While we strive for accuracy, there might be inaccuracies. Please report any issues via:
>
> - [GitHub Issues](https://github.com/funstory-ai/yadt/issues)
> - Community contribution (PRs welcome!)

## Background

After translation, text needs to be typeset before placing into PDF.

Translated paragraphs can contain any combination of the following types:

1. PDF formulas

2. Single PDF original character

3. PDF original string with same style

4. Translated Unicode string with same style

Let's discuss different cases:

For the following 3 types, they can be directly transmitted transparently to new positions:

1. PDF formulas

2. Single PDF original character

3. PDF original string with same style

Only "translated Unicode string with same style" needs typesetting operation, as this step loses original layout information. However, since paragraphs may contain other components that need transparent transmission, their positions may also change and need to participate in typesetting.

## Goal

Try to fit all components within the original paragraph bounding box. If impossible, try to expand the bounding box in writing direction.

## Specific Implementation

First perform reflow judgment to determine if the paragraph needs reflow. If all elements can be transmitted transparently, no reflow is needed. Then, if reflow is needed, execute Algorithm 1:

1. Convert all elements to typesetting unit type, which records length and width information.

2. Start from top-left of original paragraph bounding box, place elements sequentially.

3. If current line cannot fit next element, wrap to next line.

4. Repeat 2-3 until all elements are placed or exceed original bounding box.

Algorithm 1 works normally when translated text is shorter than original. When translated text is longer, Algorithm 2 needs to be added:

1. Initialize element scaling factor as 1.0.

2. Initialize line spacing as 1.5.

3. Try typesetting using Algorithm 1.

4. If it cannot fit all elements:

   - First try to reduce line spacing by 0.1 step until reaching minimum line spacing (1.4)
   - If still cannot fit:
     - When scale > 0.6, reduce element scaling by 0.05
     - When scale <= 0.6, reduce element scaling by 0.1
     - Reset line spacing to 1.5
   - When scale becomes less than 0.7, adjust minimum line spacing to 1.1

5. Report error if element scaling is less than 0.1.

Algorithm 2 can fit translations of almost all languages in original position.

However, for special cases like "图 1" translated to "Figure 1", even with the above algorithms some text may still overflow. So Algorithm 3:

1. Before reducing scale, first try to expand the bounding box in writing direction.

2. Calculate paragraph's right whitespace by:

   - Using 90% of page crop box width as maximum limit
   - Checking for overlapping paragraphs on the right
   - Checking for overlapping figures on the right

3. Expand paragraph bounding box based on available whitespace.

4. If still cannot fit all elements, continue with scale reduction as in Algorithm 2.

## Additional Features

1. Mixed Chinese-English text handling:
   - Adds 0.5 character width spacing between Chinese and English text transitions
   - Excludes certain punctuation marks from this spacing rule
2. First line indent:

   - Adds 2 Chinese characters width indent for the first line when specified

3. Hanging punctuation:
   - Allows certain punctuation marks to extend beyond the right margin
   - Helps maintain better visual alignment

## Limitations

1. Currently, we use PDFPlumber for PDF analysis, this is only implemented for paragraphs, only handles left-to-right writing.

2. Cannot handle table of contents alignment by dots.

3. Poor performance, needs optimization.

4. No global page information consideration, inconsistent text sizes.

5. No advanced typography features, poor reading experience.

## Related Resources

[UTR #59: East Asian Spacing](https://www.unicode.org/reports/tr59/) specifies which characters need spacing between them.
