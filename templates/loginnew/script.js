// Eye tracking functionality
let mouseX = 0;
let mouseY = 0;

document.addEventListener('mousemove', (e) => {
    mouseX = e.clientX;
    mouseY = e.clientY;
    updateEyes();
});

function updateEyes() {
    const eyes = document.querySelectorAll('.character-eye');
    
    eyes.forEach(eye => {
        const eyeRect = eye.getBoundingClientRect();
        const eyeCenterX = eyeRect.left + eyeRect.width / 2;
        const eyeCenterY = eyeRect.top + eyeRect.height / 2;
        
        const deltaX = mouseX - eyeCenterX;
        const deltaY = mouseY - eyeCenterY;
        const distance = Math.sqrt(deltaX * deltaX + deltaY * deltaY);
        
        // Limit eye movement within the white circle
        const maxDistance = 3;
        const moveDistance = Math.min(distance / 50, maxDistance);
        
        const angle = Math.atan2(deltaY, deltaX);
        const moveX = Math.cos(angle) * moveDistance;
        const moveY = Math.sin(angle) * moveDistance;
        
        eye.style.transform = `translate(${moveX}px, ${moveY}px)`;
    });
}

function handleLogin(event) {
    event.preventDefault();
    const email = document.getElementById('email').value;
    const password = document.getElementById('password').value;
    
    if (email && password) {
        alert(`Login attempted with email: ${email}`);
    }
}

function handleMicrosoftLogin() {
    // Show excited expressions
    const characters = document.querySelectorAll('.character-interactive');
    characters.forEach(character => {
        character.classList.add('eyes-excited');
        const mouth = character.querySelector('.character-mouth');
        mouth.classList.add('mouth-surprised');
    });
    
    setTimeout(() => {
        alert('Microsoft login clicked! This would redirect to Microsoft OAuth in a real application.');
        resetExpressions();
    }, 800);
}

function handleEmailTyping() {
    const characters = document.querySelectorAll('.character-interactive');
    characters.forEach(character => {
        character.classList.add('eyes-focused');
        const mouth = character.querySelector('.character-mouth');
        const characterId = character.id;
        
        if (characterId === 'orange-character') {
            mouth.classList.add('orange-mouth-typing');
        } else if (characterId === 'purple-character') {
            mouth.classList.add('purple-mouth-typing');
        } else if (characterId === 'black-character') {
            mouth.classList.add('black-mouth-typing');
        } else if (characterId === 'yellow-character') {
            mouth.classList.add('yellow-mouth-typing');
        }
    });
}

function handleEmailFocus() {
    const characters = document.querySelectorAll('.character-interactive');
    characters.forEach(character => {
        character.classList.add('eyes-focused');
    });
}

function handlePasswordTyping() {
    const characters = document.querySelectorAll('.character-interactive');
    characters.forEach(character => {
        character.classList.add('eyes-worried');
        const mouth = character.querySelector('.character-mouth');
        const characterId = character.id;
        
        if (characterId === 'orange-character') {
            mouth.classList.add('orange-mouth-password');
        } else if (characterId === 'purple-character') {
            mouth.classList.add('purple-mouth-password');
        } else if (characterId === 'black-character') {
            mouth.classList.add('black-mouth-password');
        } else if (characterId === 'yellow-character') {
            mouth.classList.add('yellow-mouth-password');
        }
    });
}

function handlePasswordFocus() {
    const characters = document.querySelectorAll('.character-interactive');
    characters.forEach(character => {
        character.classList.add('eyes-worried');
    });
}

function handleLoginHover() {
    const characters = document.querySelectorAll('.character-interactive');
    characters.forEach(character => {
        character.classList.add('eyes-excited');
        const mouth = character.querySelector('.character-mouth');
        const characterId = character.id;
        
        if (characterId === 'orange-character') {
            mouth.classList.add('orange-mouth-login');
        } else if (characterId === 'purple-character') {
            mouth.classList.add('purple-mouth-login');
        } else if (characterId === 'black-character') {
            mouth.classList.add('black-mouth-login');
        } else if (characterId === 'yellow-character') {
            mouth.classList.add('yellow-mouth-login');
        }
    });
}

function handleMicrosoftHover() {
    const characters = document.querySelectorAll('.character-interactive');
    characters.forEach(character => {
        character.classList.add('eyes-excited');
        const mouth = character.querySelector('.character-mouth');
        const characterId = character.id;
        
        if (characterId === 'orange-character') {
            mouth.classList.add('orange-mouth-microsoft');
        } else if (characterId === 'purple-character') {
            mouth.classList.add('purple-mouth-microsoft');
        } else if (characterId === 'black-character') {
            mouth.classList.add('black-mouth-microsoft');
        } else if (characterId === 'yellow-character') {
            mouth.classList.add('yellow-mouth-microsoft');
        }
    });
}

function resetExpressions() {
    const characters = document.querySelectorAll('.character-interactive');
    characters.forEach(character => {
        character.classList.remove('eyes-focused', 'eyes-worried', 'eyes-excited');
        const mouth = character.querySelector('.character-mouth');
        mouth.classList.remove(
            'orange-mouth-typing', 'orange-mouth-password', 'orange-mouth-login', 'orange-mouth-microsoft',
            'purple-mouth-typing', 'purple-mouth-password', 'purple-mouth-login', 'purple-mouth-microsoft',
            'black-mouth-typing', 'black-mouth-password', 'black-mouth-login', 'black-mouth-microsoft',
            'yellow-mouth-typing', 'yellow-mouth-password', 'yellow-mouth-login', 'yellow-mouth-microsoft'
        );
    });
}

function togglePasswordVisibility() {
    const passwordInput = document.getElementById('password');
    const eyeIcon = document.getElementById('eye-icon');
    
    if (passwordInput.type === 'password') {
        passwordInput.type = 'text';
        eyeIcon.innerHTML = `
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.878 9.878L3 3m6.878 6.878L21 21"></path>
        `;
    } else {
        passwordInput.type = 'password';
        eyeIcon.innerHTML = `
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path>
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"></path>
        `;
    }
}

function characterReact(character) {
    // Remove any existing animation classes
    character.classList.remove('character-bounce', 'character-wink', 'character-happy');
    
    // Add random reaction
    const reactions = ['character-bounce', 'character-wink', 'character-happy'];
    const randomReaction = reactions[Math.floor(Math.random() * reactions.length)];
    
    character.classList.add(randomReaction);
    
    // Remove the animation class after it completes
    setTimeout(() => {
        character.classList.remove(randomReaction);
    }, 1000);
}

// Enhanced blinking system
function addRandomBlink() {
    const characters = document.querySelectorAll('.character-interactive');
    characters.forEach(character => {
        if (Math.random() < 0.4) { // 40% chance to blink
            const eyes = character.querySelectorAll('.character-eye');
            eyes.forEach(eye => {
                eye.style.transform = 'scaleY(0.1)';
                setTimeout(() => {
                    eye.style.transform = '';
                }, 150);
            });
        }
    });
}

// Individual character blinking with different intervals
function startIndividualBlinking() {
    const characters = document.querySelectorAll('.character-interactive');
    characters.forEach((character, index) => {
        setInterval(() => {
            if (Math.random() < 0.6) {
                const eyes = character.querySelectorAll('.character-eye');
                eyes.forEach(eye => {
                    eye.style.transition = 'transform 0.1s ease-out';
                    eye.style.transform = 'scaleY(0.1)';
                    setTimeout(() => {
                        eye.style.transform = '';
                    }, 120);
                });
            }
        }, 2000 + (index * 500)); // Staggered timing for each character
    });
}

// Start random group blinking every 2-4 seconds
setInterval(addRandomBlink, Math.random() * 2000 + 2000);

// Start individual character blinking
startIndividualBlinking();

// Special group interactions
function startGroupHuddle() {
    const container = document.querySelector('.relative.w-72');
    container.classList.add('character-group-huddle');
    
    setTimeout(() => {
        container.classList.remove('character-group-huddle');
    }, 4000);
}

function startPeekaBoo() {
    const blackChar = document.getElementById('black-character');
    const yellowChar = document.getElementById('yellow-character');
    
    blackChar.classList.add('peek-a-boo');
    yellowChar.classList.add('peek-a-boo');
    
    setTimeout(() => {
        blackChar.classList.remove('peek-a-boo');
        yellowChar.classList.remove('peek-a-boo');
    }, 6000);
}

// Random special interactions
function triggerRandomInteraction() {
    const interactions = [startGroupHuddle, startPeekaBoo];
    const randomInteraction = interactions[Math.floor(Math.random() * interactions.length)];
    
    if (Math.random() < 0.3) { // 30% chance
        randomInteraction();
    }
}

// Trigger random group interactions every 8-12 seconds
setInterval(triggerRandomInteraction, Math.random() * 4000 + 8000);

// Initialize eye positions
updateEyes();
