package main

import (
	"context"
	"sync"
	"time"

	"go.mau.fi/whatsmeow/types"
)

// How long the number stays "online" after it last read, typed or sent.
const onlineLinger = 25 * time.Second

// presence shows the number as "online" while it answers and takes it offline
// once it has been quiet for a while, as a person who opens WhatsApp, replies
// and puts the phone down. WhatsApp shows nobody as online unless the account
// says so, and an account that stays online all the time looks automated and
// keeps the phone from notifying new messages.
type presence struct {
	send    func(context.Context, types.Presence) error
	linger  time.Duration
	onError func(error)

	mu     sync.Mutex
	online bool
	// Bumped on every activity; a timer left from an earlier one does nothing.
	turn uint64
}

func newPresence(send func(context.Context, types.Presence) error, linger time.Duration, onError func(error)) *presence {
	return &presence{send: send, linger: linger, onError: onError}
}

// active marks activity now: online if it was not, and offline again after
// linger without further activity.
func (p *presence) active(ctx context.Context) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.turn++
	turn := p.turn
	if !p.online {
		if err := p.send(ctx, types.PresenceAvailable); err != nil {
			p.onError(err)
			return
		}
		p.online = true
	}
	time.AfterFunc(p.linger, func() { p.idle(turn) })
}

func (p *presence) idle(turn uint64) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if turn != p.turn || !p.online {
		return
	}
	p.online = false
	if err := p.send(context.Background(), types.PresenceUnavailable); err != nil {
		p.onError(err)
	}
}

// forget drops what was sent before: after a reconnect the server no longer
// shows the number online, so the next activity has to say it again.
func (p *presence) forget() {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.turn++
	p.online = false
}
