/**
 * Spotlight.js
 * A vanilla JavaScript library to create a spotlight effect following the cursor.
 */
class Spotlight {
    constructor(element) {
        this.element = element;
        this.element.style.position = 'relative';
        this.element.style.overflow = 'hidden';

        this.handleMouseMove = this.handleMouseMove.bind(this);
        this.handleMouseLeave = this.handleMouseLeave.bind(this);

        this.element.addEventListener('mousemove', this.handleMouseMove);
        this.element.addEventListener('mouseleave', this.handleMouseLeave);
    }

    handleMouseMove(e) {
        const rect = this.element.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;

        this.element.style.setProperty('--spotlight-x', `${x}px`);
        this.element.style.setProperty('--spotlight-y', `${y}px`);
    }

    handleMouseLeave() {
        // You can decide what happens when the mouse leaves.
        // For now, we'll just leave the spotlight at the last position.
        // To hide it, you could set opacity to 0 or move it off-screen.
    }
}