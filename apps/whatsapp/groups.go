package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// groupWebhook hands the messages of a few chosen groups to a deployment's
// own service instead of the agent, which never sees groups. A deployment
// uses it to let its staff steer the assistant from a WhatsApp group: the
// service decides who may give orders and answers through /send. Every other
// group is still dropped. Configured with GROUP_WEBHOOK_URL,
// GROUP_WEBHOOK_TOKEN and GROUP_WEBHOOK_GROUPS (group JIDs, comma-separated);
// with any of them unset nothing changes.
type groupWebhook struct {
	url    string
	token  string
	groups map[string]bool
	client *http.Client
}

func newGroupWebhook(url, token, groups string) *groupWebhook {
	allowed := map[string]bool{}
	for _, group := range strings.Split(groups, ",") {
		if jid, err := types.ParseJID(strings.TrimSpace(group)); err == nil && jid.Server == types.GroupServer {
			allowed[jid.ToNonAD().String()] = true
		}
	}
	if url == "" || token == "" || len(allowed) == 0 {
		return nil
	}
	return &groupWebhook{url: url, token: token, groups: allowed, client: &http.Client{Timeout: 15 * time.Second}}
}

// wants reports whether a message belongs to one of the chosen groups and was
// written by someone else.
func (g *groupWebhook) wants(info types.MessageInfo) bool {
	return g != nil && info.IsGroup && !info.IsFromMe && info.ID != "" && g.groups[info.Chat.ToNonAD().String()]
}

// senderPhone is the sender's phone number when WhatsApp or the store can
// tell it: groups often address people by their anonymous @lid only.
func senderPhone(ctx context.Context, runtime *channelRuntime, source types.MessageSource) string {
	sender := source.Sender.ToNonAD()
	if sender.Server == types.DefaultUserServer {
		return sender.User
	}
	if alt := source.SenderAlt.ToNonAD(); alt.Server == types.DefaultUserServer {
		return alt.User
	}
	if sender.Server == types.HiddenUserServer {
		if pn, err := runtime.client.Store.LIDs.GetPNForLID(ctx, sender); err == nil && !pn.IsEmpty() {
			return pn.User
		}
	}
	return ""
}

func mentionedJIDs(msg *waE2E.Message) []string {
	content := unwrapMessage(msg)
	if content == nil {
		return nil
	}
	if text := content.GetExtendedTextMessage(); text != nil {
		return text.GetContextInfo().GetMentionedJID()
	}
	return nil
}

func (m *manager) forwardGroupMessage(ctx context.Context, runtime *channelRuntime, evt *events.Message) error {
	text := incomingText(evt.Message)
	media := incomingMedia(evt.Message)
	if text == "" && media == nil {
		return nil
	}
	body := map[string]any{
		"channel_id":   runtime.channelID,
		"group_jid":    evt.Info.Chat.ToNonAD().String(),
		"message_id":   evt.Info.ID,
		"sender_jid":   evt.Info.Sender.ToNonAD().String(),
		"sender_phone": senderPhone(ctx, runtime, evt.Info.MessageSource),
		"sender_name":  evt.Info.PushName,
		"text":         text,
		"mentions":     mentionedJIDs(evt.Message),
		"timestamp":    evt.Info.Timestamp.Unix(),
	}
	if quotedID := incomingQuotedID(evt.Message); quotedID != "" {
		body["quoted_id"] = quotedID
	}
	if own := runtime.client.Store.ID; own != nil {
		body["own_jid"] = own.ToNonAD().String()
	}
	if lid := runtime.client.Store.LID; !lid.IsEmpty() {
		body["own_lid"] = lid.ToNonAD().String()
	}
	// Voice notes carry the instruction themselves; other media is announced only.
	if media != nil {
		body["media_kind"] = media.kind
		if media.kind == "audio" && media.length <= maxMediaBytes {
			if data, err := runtime.client.Download(ctx, media.download); err == nil && len(data) <= maxMediaBytes {
				body["media_mime"] = media.mimetype
				body["media_base64"] = base64.StdEncoding.EncodeToString(data)
			} else if err != nil {
				m.log.Errorf("channel %s: could not download group audio: %v", runtime.channelID, err)
			}
		}
	}
	data, err := json.Marshal(body)
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, m.groups.url, bytes.NewReader(data))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-Group-Webhook-Token", m.groups.token)
	response, err := m.groups.client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode >= 400 {
		return fmt.Errorf("group webhook responded with %d", response.StatusCode)
	}
	return nil
}
