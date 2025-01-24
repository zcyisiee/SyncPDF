# Typography

## Background

After translation, text needs to be typeset before placing into PDF.

Translated paragraphs can contain any combination of the following types:

1. PDF formulas

2. Single PDF original character

3. PDF original string with same style

4. Translated unicode string with same style

Let's discuss different cases:

For the following 3 types, they can be directly transmitted transparently to new positions:

1. PDF formulas

2. Single PDF original character

3. PDF original string with same style

Only "translated unicode string with same style" needs typesetting operation, as this step loses original layout information. However, since paragraphs may contain other components that need transparent transmission, their positions may also change and need to participate in typesetting.

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

2. Initialize line spacing as 1.7.

3. Try typesetting using Algorithm 1.

4. If cannot fit all elements, first reduce line spacing by 0.1 step. If still cannot fit after reaching 1.1 spacing, reduce element scaling by 0.05 then jump to step 2.

5. Report error if element scaling is less than 0.1.

Algorithm 2 can fit translations of almost all languages in original position.

However for special cases like "图 1" translated to "Figure 1", even 0.1 scaling cannot fit, need to try expanding bounding box in writing direction. So Algorithm 3:

1. Try typesetting with minimum 0.8 scaling.

2. If cannot fit all elements, calculate paragraph's right whitespace using page information.

3. Expand paragraph bounding box based on whitespace.

4. Try typesetting with minimum 0.1 scaling.

## Limitations

1. Currently only handles left-to-right writing.

2. Cannot handle table of contents alignment by dots.

3. Poor performance, needs optimization.

4. No global page information consideration, inconsistent text sizes.

5. No advanced typography features, poor reading experience.