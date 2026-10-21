"use client";

import { useEffect, useState } from "react";
import { CalendarClock, CheckCircle2, ChevronDown, XCircle } from "lucide-react";
import { ConfirmModal } from "@/components/confirm-modal";
import { api, messageFrom } from "@/lib/api";
import { useT } from "@/lib/i18n";

type Booking = {
  id: string;
  title: string;
  when: string;
  status: string;
  detail: string | null;
  can_cancel: boolean;
  can_mark_paid: boolean;
};
type Bookings = { enabled: boolean; bookings: Booking[] };
type Action = "cancel" | "paid";

/** The contact's bookings in the outside booking service (BOOKINGS_URL),
 * pinned above the thread with Cancel and Mark as paid. Hidden when no service
 * is set up or the contact has no booking. The service words each booking;
 * this card lays it out and asks before acting. */
export function ConversationBookings({ slug, conversationId }: { slug: string; conversationId: string }) {
  const t = useT();
  const [data, setData] = useState<Bookings | null>(null);
  const [open, setOpen] = useState(true);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<{ booking: Booking; action: Action } | null>(null);
  const path = `/portal/${slug}/conversations/${conversationId}/bookings`;

  useEffect(() => {
    let live = true;
    setData(null); setError(""); setPending(null); setOpen(true);
    api<Bookings>(path)
      .then((value) => { if (live) setData(value); })
      .catch((err) => { if (live) setError(messageFrom(err)); });
    return () => { live = false; };
  }, [path]);

  async function act(booking: Booking, action: Action) {
    setData(await api<Bookings>(`${path}/${encodeURIComponent(booking.id)}/${action}`, { method: "POST" }));
  }

  if (!error && (!data?.enabled || data.bookings.length === 0)) return null;

  return (
    <section className={open ? "conversation-bookings open" : "conversation-bookings"} aria-label={t("portal.inbox.conversation.bookingsTitle")}>
      <button type="button" className="conversation-bookings-toggle" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <CalendarClock size={15} />
        <strong>{t("portal.inbox.conversation.bookingsTitle")}</strong>
        {data && <small>{data.bookings.length}</small>}
        <ChevronDown size={15} className="conversation-bookings-chevron" />
      </button>
      {open && <div className="conversation-bookings-body">
        {error && <p className="conversation-bookings-error">{error}</p>}
        {data?.bookings.map((booking) => (
          <div key={booking.id} className="conversation-booking">
            <div className="conversation-booking-text">
              <strong>{booking.title}</strong>
              <small>{[booking.when, booking.status].filter(Boolean).join(" · ")}</small>
              {booking.detail && <small className="muted">{booking.detail}</small>}
            </div>
            <div className="conversation-booking-actions">
              {booking.can_mark_paid && <button type="button" className="button ghost small" onClick={() => setPending({ booking, action: "paid" })}><CheckCircle2 size={13} /> {t("portal.inbox.conversation.bookingsMarkPaid")}</button>}
              {booking.can_cancel && <button type="button" className="button ghost small" onClick={() => setPending({ booking, action: "cancel" })}><XCircle size={13} /> {t("portal.inbox.conversation.bookingsCancel")}</button>}
            </div>
          </div>
        ))}
      </div>}
      {pending && <ConfirmModal
        title={t(pending.action === "cancel" ? "portal.inbox.conversation.bookingsCancelTitle" : "portal.inbox.conversation.bookingsPaidTitle")}
        message={<><strong>{pending.booking.title}</strong>{pending.booking.when ? ` · ${pending.booking.when}` : ""}<br />{t(pending.action === "cancel" ? "portal.inbox.conversation.bookingsCancelCopy" : "portal.inbox.conversation.bookingsPaidCopy")}</>}
        confirmLabel={t(pending.action === "cancel" ? "portal.inbox.conversation.bookingsCancel" : "portal.inbox.conversation.bookingsMarkPaid")}
        cancelLabel={t("common.back")}
        danger={pending.action === "cancel"}
        onConfirm={() => act(pending.booking, pending.action)}
        onClose={() => setPending(null)}
      />}
    </section>
  );
}
