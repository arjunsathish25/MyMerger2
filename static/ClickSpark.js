/**
 * ClickSpark.js
 * A vanilla JavaScript library to create a click-spark effect.
 *
 * Original React component by Josh W. Comeau.
 * Vanilla JS adaptation for this project.
 */
class ClickSpark {
  constructor({
    selector = 'body',
    sparkColor = 'hsla(39, 100%, 50%, 1.00)',
    sparkSize = 10,
    sparkRadius = 25,
    sparkCount = 8,
    duration = 500,
    easing = 'ease-out',
    extraScale = 1.0,
  } = {}) {
    this.sparkColor = sparkColor;
    this.sparkSize = sparkSize;
    this.sparkRadius = sparkRadius;
    this.sparkCount = sparkCount;
    this.duration = duration;
    this.easing = easing;
    this.extraScale = extraScale;

    this.sparks = [];
    this.startTime = null;

    this.wrapper = document.querySelector(selector);
    if (!this.wrapper) {
      console.error(`ClickSpark: Element with selector "${selector}" not found.`);
      return;
    }

    this.canvas = document.createElement('canvas');
    this.ctx = this.canvas.getContext('2d');
    this.initCanvas();

    this.wrapper.style.position = this.wrapper.style.position || 'relative';
    this.wrapper.appendChild(this.canvas);

    this.wrapper.addEventListener('click', this.handleClick.bind(this));
    this.resizeObserver = new ResizeObserver(() => this.resizeCanvas());
    this.resizeObserver.observe(this.wrapper);

    this.draw = this.draw.bind(this);
    requestAnimationFrame(this.draw);
  }

  initCanvas() {
    this.canvas.style.position = 'absolute';
    this.canvas.style.top = '0';
    this.canvas.style.left = '0';
    this.canvas.style.width = '100%';
    this.canvas.style.height = '100%';
    this.canvas.style.pointerEvents = 'none';
    this.canvas.style.userSelect = 'none';
    this.canvas.style.zIndex = '999';
    this.resizeCanvas();
  }

  resizeCanvas() {
    const { width, height } = this.wrapper.getBoundingClientRect();
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
    }
  }

  easeFunc(t) {
    switch (this.easing) {
      case 'linear': return t;
      case 'ease-in': return t * t;
      case 'ease-in-out': return t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
      default: return t * (2 - t); // ease-out
    }
  }

  draw(timestamp) {
    if (!this.startTime) {
      this.startTime = timestamp;
    }
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);

    this.sparks = this.sparks.filter(spark => {
      const elapsed = timestamp - spark.startTime;
      if (elapsed >= this.duration) {
        return false;
      }

      const progress = elapsed / this.duration;
      const eased = this.easeFunc(progress);

      const distance = eased * this.sparkRadius * this.extraScale;
      const lineLength = this.sparkSize * (1 - eased);

      const x1 = spark.x + distance * Math.cos(spark.angle);
      const y1 = spark.y + distance * Math.sin(spark.angle);
      const x2 = spark.x + (distance + lineLength) * Math.cos(spark.angle);
      const y2 = spark.y + (distance + lineLength) * Math.sin(spark.angle);

      this.ctx.strokeStyle = this.sparkColor;
      this.ctx.lineWidth = 2;
      this.ctx.beginPath();
      this.ctx.moveTo(x1, y1);
      this.ctx.lineTo(x2, y2);
      this.ctx.stroke();

      return true;
    });

    requestAnimationFrame(this.draw);
  }

  handleClick(e) {
    const rect = this.canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    const now = performance.now();
    const newSparks = Array.from({ length: this.sparkCount }, (_, i) => ({
      x,
      y,
      angle: (2 * Math.PI * i) / this.sparkCount,
      startTime: now
    }));

    this.sparks.push(...newSparks);
  }

  destroy() {
    this.wrapper.removeEventListener('click', this.handleClick);
    this.resizeObserver.disconnect();
    this.wrapper.removeChild(this.canvas);
  }
}