package main

import (
	"context"
	"errors"
	"slices"
	"sync"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/types"
)

type presenceLog struct {
	mu     sync.Mutex
	states []types.Presence
	fail   bool
}

func (l *presenceLog) send(_ context.Context, state types.Presence) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.fail {
		return errors.New("no push name")
	}
	l.states = append(l.states, state)
	return nil
}

func (l *presenceLog) seen() []types.Presence {
	l.mu.Lock()
	defer l.mu.Unlock()
	return slices.Clone(l.states)
}

func (l *presenceLog) failing(fail bool) {
	l.mu.Lock()
	l.fail = fail
	l.mu.Unlock()
}

const testLinger = 60 * time.Millisecond

func newTestPresence(log *presenceLog) (*presence, *[]error) {
	var errs []error
	return newPresence(log.send, testLinger, func(err error) { errs = append(errs, err) }), &errs
}

func TestPresenceGoesOnlineOnceAndOfflineAfterQuiet(t *testing.T) {
	log := &presenceLog{}
	p, _ := newTestPresence(log)
	p.active(context.Background())
	p.active(context.Background())
	if got := log.seen(); !slices.Equal(got, []types.Presence{types.PresenceAvailable}) {
		t.Fatalf("after two activities: %v", got)
	}
	time.Sleep(testLinger * 2)
	if got := log.seen(); !slices.Equal(got, []types.Presence{types.PresenceAvailable, types.PresenceUnavailable}) {
		t.Fatalf("after the quiet period: %v", got)
	}
}

func TestPresenceActivityPostponesOffline(t *testing.T) {
	log := &presenceLog{}
	p, _ := newTestPresence(log)
	p.active(context.Background())
	time.Sleep(testLinger / 2)
	p.active(context.Background())
	time.Sleep(testLinger * 3 / 4)
	if got := log.seen(); len(got) != 1 {
		t.Fatalf("went offline although there was activity %v ago: %v", testLinger*3/4, got)
	}
	time.Sleep(testLinger)
	if got := log.seen(); !slices.Equal(got, []types.Presence{types.PresenceAvailable, types.PresenceUnavailable}) {
		t.Fatalf("after the quiet period: %v", got)
	}
}

func TestPresenceRetriesWhenOnlineFails(t *testing.T) {
	log := &presenceLog{fail: true}
	p, errs := newTestPresence(log)
	p.active(context.Background())
	time.Sleep(testLinger * 2)
	if got := log.seen(); len(got) != 0 || len(*errs) != 1 {
		t.Fatalf("a failed online must not be followed by offline: %v, errors %v", got, *errs)
	}
	log.failing(false)
	p.active(context.Background())
	if got := log.seen(); !slices.Equal(got, []types.Presence{types.PresenceAvailable}) {
		t.Fatalf("the next activity must try again: %v", got)
	}
}

func TestPresenceForgetSendsOnlineAgain(t *testing.T) {
	log := &presenceLog{}
	p, _ := newTestPresence(log)
	p.active(context.Background())
	p.forget()
	time.Sleep(testLinger * 2)
	if got := log.seen(); len(got) != 1 {
		t.Fatalf("a timer from before the reconnect must do nothing: %v", got)
	}
	p.active(context.Background())
	if got := log.seen(); !slices.Equal(got, []types.Presence{types.PresenceAvailable, types.PresenceAvailable}) {
		t.Fatalf("after a reconnect the number must say it is online again: %v", got)
	}
}
