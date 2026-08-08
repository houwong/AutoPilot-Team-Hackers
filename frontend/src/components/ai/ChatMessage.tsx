'use client'

import React from 'react'
import { cn } from '@/lib/utils'
import { Icons } from '@/components/ui/icons'
import { Avatar } from '@/components/ui/avatar'
import type { ChatMessage as ChatMessageType } from '@/context/AIContext'

interface ChatMessageProps {
  message: ChatMessageType
  userName?: string
  userImage?: string | null
}

function formatTime(date: Date): string {
  return date.toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  })
}

/**
 * Render inline markdown (bold, code, italic) in text
 */
function renderInlineMarkdown(text: string): React.ReactNode[] {
  const parts: React.ReactNode[] = []
  // Match **bold**, `code`, and *italic*
  const regex = /(\*\*[^*]+\*\*)|(`[^`]+`)|(\*[^*]+\*)/g
  let lastIndex = 0
  let match
  let keyIndex = 0

  while ((match = regex.exec(text)) !== null) {
    // Add text before match
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index))
    }

    const segment = match[0]
    if (segment.startsWith('**') && segment.endsWith('**')) {
      // Bold
      parts.push(
        <strong key={keyIndex++} className="font-semibold text-foreground">
          {segment.slice(2, -2)}
        </strong>
      )
    } else if (segment.startsWith('`') && segment.endsWith('`')) {
      // Code
      parts.push(
        <code
          key={keyIndex++}
          className="px-1.5 py-0.5 bg-muted/50 rounded text-xs font-mono text-brand-navy"
        >
          {segment.slice(1, -1)}
        </code>
      )
    } else if (segment.startsWith('*') && segment.endsWith('*')) {
      // Italic
      parts.push(
        <em key={keyIndex++} className="italic">
          {segment.slice(1, -1)}
        </em>
      )
    }
    lastIndex = regex.lastIndex
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex))
  }

  return parts.length > 0 ? parts : [text]
}

/**
 * Render markdown content with support for headers, lists, blockquotes
 */
function MarkdownContent({ content }: { content: string }) {
  const lines = content.split('\n')
  
  return (
    <div className="space-y-2 text-sm leading-relaxed">
      {lines.map((line, i) => {
        const trimmedLine = line.trim()
        
        // Empty line - add spacing
        if (!trimmedLine) {
          return <div key={i} className="h-1" />
        }
        
        // Headers (##, ###)
        if (trimmedLine.startsWith('### ')) {
          return (
            <h4 key={i} className="font-semibold text-foreground mt-3 first:mt-0">
              {renderInlineMarkdown(trimmedLine.slice(4))}
            </h4>
          )
        }
        if (trimmedLine.startsWith('## ')) {
          return (
            <h3 key={i} className="font-bold text-foreground mt-3 first:mt-0">
              {renderInlineMarkdown(trimmedLine.slice(3))}
            </h3>
          )
        }
        
        // Blockquote
        if (trimmedLine.startsWith('> ')) {
          return (
            <blockquote
              key={i}
              className="pl-3 border-l-2 border-brand-cornflower/50 text-muted-foreground italic"
            >
              {renderInlineMarkdown(trimmedLine.slice(2))}
            </blockquote>
          )
        }
        
        // Bullet points (* or -)
        if (/^[*\-]\s/.test(trimmedLine)) {
          return (
            <div key={i} className="flex gap-2 pl-1">
              <span className="w-1.5 h-1.5 rounded-full bg-brand-cornflower/60 mt-2 flex-shrink-0" />
              <span>{renderInlineMarkdown(trimmedLine.replace(/^[*\-]\s/, ''))}</span>
            </div>
          )
        }
        
        // Numbered lists
        if (/^\d+\.\s/.test(trimmedLine)) {
          const match = trimmedLine.match(/^(\d+)\.\s(.*)/)
          if (match) {
            return (
              <div key={i} className="flex gap-2 pl-1">
                <span className="text-brand-cornflower font-medium min-w-[1.25rem]">
                  {match[1]}.
                </span>
                <span>{renderInlineMarkdown(match[2])}</span>
              </div>
            )
          }
        }
        
        // Regular paragraph
        return <p key={i}>{renderInlineMarkdown(trimmedLine)}</p>
      })}
    </div>
  )
}

export function ChatMessage({ message, userName, userImage }: ChatMessageProps) {
  const isUser = message.role === 'user'
  const isLoading = message.isLoading

  return (
    <div
      className={cn(
        'flex gap-3',
        isUser ? 'flex-row-reverse' : 'flex-row'
      )}
    >
      {/* Avatar */}
      <div className="flex-shrink-0">
        {isUser ? (
          <Avatar
            src={userImage}
            fallback={userName || 'U'}
            size="sm"
          />
        ) : (
          <div className={cn(
            'flex h-8 w-8 items-center justify-center rounded-full',
            'bg-gradient-to-br from-brand-navy to-brand-purple',
            'shadow-soft'
          )}>
            <Icons.sparkles className="h-4 w-4 text-white" strokeWidth={1.5} />
          </div>
        )}
      </div>

      {/* Message Content */}
      <div className={cn(
        'flex max-w-[85%] flex-col gap-1',
        isUser ? 'items-end' : 'items-start'
      )}>
        <div
          className={cn(
            'rounded-2xl px-4 py-3',
            'transition-all duration-200',
            isUser
              ? 'bg-brand-navy text-white rounded-br-md'
              : 'bg-white/90 text-foreground border border-border/50 rounded-bl-md shadow-soft',
            isLoading && 'animate-pulse'
          )}
        >
          {isLoading ? (
            <div className="flex items-center gap-2 py-1">
              {/* Fade, not bounce. Bounce easing overshoots and settles like a
                  rubber ball — playful, and the wrong register for an
                  operations tool. The tell is loudest on a waiting indicator,
                  because that is the one thing the eye rests on. */}
              <div className="flex gap-1">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand-muted/70" style={{ animationDelay: '0ms', animationDuration: '1.2s' }} />
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand-muted/70" style={{ animationDelay: '200ms', animationDuration: '1.2s' }} />
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand-muted/70" style={{ animationDelay: '400ms', animationDuration: '1.2s' }} />
              </div>
              <span className="text-sm text-brand-muted">Reading the records…</span>
            </div>
          ) : isUser ? (
            <p className="text-sm whitespace-pre-wrap leading-relaxed">
              {message.content}
            </p>
          ) : (
            <MarkdownContent content={message.content} />
          )}
        </div>

        {/* Timestamp */}
        <span className={cn(
          'text-[10px] text-muted-foreground/60',
          isUser ? 'pr-1' : 'pl-1'
        )}>
          {formatTime(message.timestamp)}
        </span>

        {/* Tool calls indicator */}
        {message.toolCalls && message.toolCalls.length > 0 && (
          <div className="flex items-center gap-1.5 text-[10px] text-brand-cornflower pl-1">
            <Icons.zap className="h-3 w-3" strokeWidth={1.5} />
            <span>Used {message.toolCalls.length} tool{message.toolCalls.length > 1 ? 's' : ''}</span>
          </div>
        )}
      </div>
    </div>
  )
}
