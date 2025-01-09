import labels from "./labels.json";
import Tesseract from 'tesseract.js';


// Helper functions for text segmentation
const isCJK = (text) => {
  const regex = /[\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF]/;
  return regex.test(text);
};

const splitText = (text) => {
  const result = [];
  let current = '';

  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    
    if (isCJK(char)) {
      if (current) {
        result.push(current);
        current = '';
      }
      result.push(char);
    } else {
      if (char === ' ') {
        if (current) {
          result.push(current);
          current = '';
        }
      } else {
        current += char;
      }
    }
  }

  if (current) {
    result.push(current);
  }

  return result;
};

/**
 * Render prediction boxes and perform OCR
 * @param {CanvasRenderingContext2D} ctx context
 * @param {Array[Object]} boxes boxes array
 */
export const renderBoxes = async (ctx, boxes, debug) => {
  const colors = new Colors();
  
  const timings = {
    ocr: 0,
    translation: 0
  };

  const font = `${Math.max(
    Math.round(Math.max(ctx.canvas.width, ctx.canvas.height) / 40),
    14
  )}px Arial`;
  ctx.font = font;
  ctx.textBaseline = "top";

  // OCR timing start
  const ocrStart = performance.now();
  const ocrPromises = boxes.map(async box => {
    const [x1, y1, width, height] = box.bounding;
    const imageData = ctx.getImageData(x1, y1, width, height);
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const tempCtx = canvas.getContext('2d');
    tempCtx.putImageData(imageData, 0, 0);

    try {
      const { data: { text } } = await Tesseract.recognize(canvas);
      return {
        ...box,
        originalText: text.trim().replace(/\s+/g, ' ')
      };
    } catch(err) {
      console.error('OCR failed:', err);
      return {
        ...box,
        originalText: ''
      };
    }
  });

  let boxesWithText = await Promise.all(ocrPromises);
  timings.ocr = performance.now() - ocrStart;

  const textsToTranslate = boxesWithText
    .map(box => box.originalText)
    .filter(text => text);

  let translations = [];
  if (textsToTranslate.length > 0) {
    const translationStart = performance.now();
    try {
      const response = await fetch('https://api2.immersivetranslate.com/deepl/translate', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'token': 'temp-token-2023'
        },
        body: JSON.stringify({
          text: textsToTranslate,
          source_lang: 'EN',
          target_lang: 'ZH'
        })
      });
      
      const data = await response.json();
      translations = data.translations?.map(t => t.text) || textsToTranslate;
    } catch (err) {
      console.error('Translation failed:', err);
      translations = textsToTranslate;
    }
    timings.translation = performance.now() - translationStart;
  }

  // Add translations back to boxes
  let translationIndex = 0;
  boxesWithText = boxesWithText.map(box => {
    if (box.originalText) {
      return {
        ...box,
        translatedText: translations[translationIndex++]
      };
    }
    return box;
  });

  // Render boxes and translations
  boxesWithText.forEach(box => {
    const color = colors.get(box.label);
    const [x1, y1, width, height] = box.bounding;

    // Get image data for color analysis
    const imageData = ctx.getImageData(x1, y1, width, height);
    const data = imageData.data;
    const colorCounts = new Map();

    // Process each pixel
    for (let i = 0; i < data.length; i += 4) {
      const r = Math.round(data[i] / 16) * 16;
      const g = Math.round(data[i + 1] / 16) * 16;
      const b = Math.round(data[i + 2] / 16) * 16;
      const key = `rgb(${r},${g},${b})`;
      
      colorCounts.set(key, (colorCounts.get(key) || 0) + 1);
    }

    // Sort colors by frequency
    const sortedColors = [...colorCounts.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 2);

    const [topColor, secondColor] = sortedColors;
    // if primary and secondary too close, make secondary pure white or black

    const getBrightness = (color) => {
      const rgb = color.match(/\d+/g);
      return (parseInt(rgb[0]) * 299 + parseInt(rgb[1]) * 587 + parseInt(rgb[2]) * 114) / 1000;
    };

    if (topColor && secondColor) {
      const primaryBrightness = getBrightness(topColor[0]);
      const secondaryBrightness = getBrightness(secondColor[0]);
      const brightnessDiff = Math.abs(primaryBrightness - secondaryBrightness);
      
      if (brightnessDiff < 50) {
        // If colors are too similar, make secondary either white or black
        secondColor[0] = primaryBrightness > 128 ? 'rgb(0,0,0)' : 'rgb(255,255,255)';
      }
    }
    
    box.dominantColors = {
      primary: topColor ? topColor[0] : null,
      secondary: secondColor ? secondColor[0] : null
    };
    

    // Draw box
    if (debug) {
      ctx.strokeStyle = color;
      ctx.lineWidth = Math.max(Math.min(ctx.canvas.width, ctx.canvas.height) / 200, 2.5);
      ctx.strokeRect(x1, y1, width, height);
      ctx.fillStyle = Colors.hexToRgba(color, 0.2);
      ctx.fillRect(x1, y1, width, height);
    }

    // Draw translated text if available
    if (box.translatedText) {
      // Calculate initial font size based on box dimensions and text length
      const MIN_FONT_SIZE = 12;
      const MAX_FONT_SIZE = 256;
      const FONT_STEP = 2;
      const PADDING = 8;

      const AREA_PER_CHAR = 400; // baseline pixels^2 per character
      
      const boxArea = width * height;
      const charCount = box.translatedText.length;
      const areaPerChar = boxArea / charCount;
      
      // Initial font size calculation:
      // - Starts with square root of area per character
      // - Scaled down by a factor to account for padding and line spacing
      let fontSize = Math.sqrt(areaPerChar) * 0.7;
      
      // Clamp to min/max bounds
      fontSize = Math.max(MIN_FONT_SIZE, Math.min(fontSize, MAX_FONT_SIZE));
      fontSize = Math.min(fontSize, height / 2); // Never larger than half box height

      while (fontSize < MAX_FONT_SIZE) {
        ctx.font = `${fontSize}px Arial`;
        const lineHeight = fontSize * 1.2;
        
        // Continue with line breaking logic...
        const maxWidth = width - PADDING; // Added more padding

        const segments = splitText(box.translatedText);
        const lines = [];
        let currentLine = '';
        let last = '';
        let split = false;
        
        segments.forEach(segment => {
            split = currentLine !== '' && !isCJK(last) && !isCJK(segment);
            const testLine = currentLine + (split ? ' ' : '') + segment;
            last = segment;
            const metrics = ctx.measureText(testLine);
            if (metrics.width > maxWidth && currentLine !== '') {
                lines.push(currentLine);
                currentLine = segment;
            } else {
                currentLine = testLine;
            }
        });
        
        if (currentLine) {
            lines.push(currentLine);
        }

        // Draw text background and text
        const totalHeight = lines.length * lineHeight;
        const startY = y1 + (height - totalHeight) / 2;


        ctx.fillStyle = box.dominantColors.primary || '#000000';
        ctx.fillRect(x1, y1, width, height);

        lines.forEach((line, i) => {
          const textWidth = ctx.measureText(line).width;
          const textX = x1 + (width - textWidth) / 2;
          const textY = startY + i * lineHeight;

          // Draw text
          ctx.fillStyle = box.dominantColors.secondary || '#ffffff';
          ctx.fillText(line, textX, textY);
        });

        if (totalHeight >= height - PADDING) {
          break;
        }

        fontSize += FONT_STEP;

      }
      
    }
  });

  return timings;
};

class Colors {
  // ultralytics color palette https://ultralytics.com/
  constructor() {
    this.palette = [
      "#FF3838",
      "#FF9D97",
      "#FF701F",
      "#FFB21D",
      "#CFD231",
      "#48F90A",
      "#92CC17",
      "#3DDB86",
      "#1A9334",
      "#00D4BB",
      "#2C99A8",
      "#00C2FF",
      "#344593",
      "#6473FF",
      "#0018EC",
      "#8438FF",
      "#520085",
      "#CB38FF",
      "#FF95C8",
      "#FF37C7",
    ];
    this.n = this.palette.length;
  }

  get = (i) => this.palette[Math.floor(i) % this.n];

  static hexToRgba = (hex, alpha) => {
    var result = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
    return result
      ? `rgba(${[
          parseInt(result[1], 16),
          parseInt(result[2], 16),
          parseInt(result[3], 16),
        ].join(", ")}, ${alpha})`
      : null;
  };
}
