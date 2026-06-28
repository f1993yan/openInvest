package com.f1993yan.openInvest

import android.animation.Animator
import android.animation.AnimatorListenerAdapter
import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.os.Handler
import android.os.Looper
import android.util.AttributeSet
import android.util.TypedValue
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.animation.DecelerateInterpolator
import android.view.animation.OvershootInterpolator
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

class DynamicIslandView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : FrameLayout(context, attrs, defStyleAttr) {

    private val collapsedWidth = dpToPx(240)
    private val collapsedHeight = dpToPx(38)
    private val expandedWidth = dpToPx(320)
    private val expandedHeight = dpToPx(176) // Sized down to 176dp since the bottom button is removed

    var isExpanded = false
        private set

    private var currentType = IslandType.MONITORING

    // Views
    private val collapsedLayout: LinearLayout
    private val collapsedIcon: ImageView
    private val collapsedText: TextView
    private val collapsedBadge: TextView

    private val expandedLayout: LinearLayout
    private val expandedTitleText: TextView
    private val expandedProgressText: TextView

    // Progress line container
    private val progressTrackLayout: FrameLayout
    private val segmentsContainer: LinearLayout
    private val progressWhale: ImageView

    private val detailsScroll: ScrollView
    private val detailsListLayout: LinearLayout

    private var onOpenAppClickListener: (() -> Unit)? = null
    private var onSizeChangedListener: ((width: Int, height: Int) -> Unit)? = null

    init {
        // Outer layout settings
        clipChildren = true
        clipToPadding = true

        // Background card - Deep space black with a subtle premium outline
        val bgDrawable = GradientDrawable().apply {
            setColor(Color.parseColor("#090D16")) // Elegant deep space black
            cornerRadius = dpToPx(19).toFloat() // Perfect circular pill corners for 38dp height
            setStroke(dpToPx(1), Color.parseColor("#27272A")) // Premium Zinc-800 border outline
        }
        background = bgDrawable

        // 1. Collapsed Layout
        collapsedLayout = LinearLayout(context).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dpToPx(12), 0, dpToPx(12), 0)
            alpha = 1.0f
        }

        // Circular frame for the whale icon to keep it neat
        collapsedIcon = ImageView(context).apply {
            setImageResource(R.drawable.deepseek_whale)
            val params = LinearLayout.LayoutParams(dpToPx(22), dpToPx(22))
            params.rightMargin = dpToPx(8)
            layoutParams = params
            scaleType = ImageView.ScaleType.FIT_CENTER

            // Premium circular framing
            val shape = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(Color.parseColor("#1E293B")) // Slate-800 background
            }
            background = shape
            setPadding(dpToPx(2), dpToPx(2), dpToPx(2), dpToPx(2))
        }

        collapsedText = TextView(context).apply {
            text = "后台数据监控中"
            setTextColor(Color.parseColor("#F4F4F5")) // Clean off-white
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            typeface = android.graphics.Typeface.create("sans-serif-medium", android.graphics.Typeface.NORMAL)
            ellipsize = android.text.TextUtils.TruncateAt.END
            isSingleLine = true
            val params = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1.0f)
            layoutParams = params
        }

        // Beautiful pill badge with colored background matching current state
        collapsedBadge = TextView(context).apply {
            text = "监控中"
            setTextColor(Color.parseColor("#10B981")) // Emerald text
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 9f)
            typeface = android.graphics.Typeface.create("sans-serif-bold", android.graphics.Typeface.BOLD)
            setPadding(dpToPx(8), dpToPx(3), dpToPx(8), dpToPx(3))

            val badgeBg = GradientDrawable().apply {
                setColor(Color.parseColor("#1A10B981")) // Emerald background with 10% alpha
                cornerRadius = dpToPx(10).toFloat()
            }
            background = badgeBg

            val params = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT)
            params.leftMargin = dpToPx(4)
            layoutParams = params
        }

        collapsedLayout.addView(collapsedIcon)
        collapsedLayout.addView(collapsedText)
        collapsedLayout.addView(collapsedBadge)
        addView(collapsedLayout, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))

        // 2. Expanded Layout
        expandedLayout = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dpToPx(16), dpToPx(14), dpToPx(16), dpToPx(14))
            visibility = View.GONE
            alpha = 0.0f
        }

        // Header Title
        expandedTitleText = TextView(context).apply {
            text = "OpenInvest 智能决策中心"
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
            typeface = android.graphics.Typeface.create("sans-serif-bold", android.graphics.Typeface.BOLD)
        }
        expandedLayout.addView(expandedTitleText)

        // Progress Text
        expandedProgressText = TextView(context).apply {
            text = "等待分析评估启动..."
            setTextColor(Color.parseColor("#A1A1AA")) // Zinc-400 text
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
            val params = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT)
            params.topMargin = dpToPx(6)
            params.bottomMargin = dpToPx(6)
            layoutParams = params
        }
        expandedLayout.addView(expandedProgressText)

        // Progress Track containing N segments & the hopping whale
        progressTrackLayout = FrameLayout(context).apply {
            val params = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dpToPx(24))
            params.topMargin = dpToPx(2)
            params.bottomMargin = dpToPx(6)
            layoutParams = params
            clipChildren = false
            clipToPadding = false
        }

        // Segments container (N small horizontal bars)
        segmentsContainer = LinearLayout(context).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            val params = LayoutParams(LayoutParams.MATCH_PARENT, dpToPx(6))
            params.gravity = Gravity.CENTER_VERTICAL
            layoutParams = params
        }
        progressTrackLayout.addView(segmentsContainer)

        // DeepSeek swimming whale icon in the progress track
        progressWhale = ImageView(context).apply {
            setImageResource(R.drawable.deepseek_whale)
            val params = LayoutParams(dpToPx(22), dpToPx(22))
            params.gravity = Gravity.LEFT or Gravity.CENTER_VERTICAL
            layoutParams = params
            scaleType = ImageView.ScaleType.FIT_CENTER

            // Glowing border framing
            val shape = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(Color.parseColor("#10141D"))
                setStroke(dpToPx(1), Color.parseColor("#6366F1"))
            }
            background = shape
            setPadding(dpToPx(2), dpToPx(2), dpToPx(2), dpToPx(2))
        }
        progressTrackLayout.addView(progressWhale)
        expandedLayout.addView(progressTrackLayout)

        // Scroll view for alerts list
        detailsScroll = ScrollView(context).apply {
            isVerticalScrollBarEnabled = false
            overScrollMode = View.OVER_SCROLL_NEVER
        }
        detailsListLayout = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
        }
        detailsScroll.addView(detailsListLayout)
        val scrollParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)
        scrollParams.topMargin = dpToPx(4)
        expandedLayout.addView(detailsScroll, scrollParams)

        addView(expandedLayout, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))

        // Toggle / Launch Tap listener
        setOnClickListener {
            if (isExpanded) {
                // If clicked while expanded, launch the app directly!
                onOpenAppClickListener?.invoke()
            } else {
                // If clicked while collapsed, expand to show details!
                expand()
            }
        }

        // Set initial view size
        layoutParams = LayoutParams(collapsedWidth, collapsedHeight).apply {
            gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
        }
    }

    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        // Collapse automatically if the user clicks anywhere outside the expanded floating window
        if (ev.action == MotionEvent.ACTION_OUTSIDE) {
            if (isExpanded) {
                collapse()
            }
            return true
        }
        return super.dispatchTouchEvent(ev)
    }

    fun setOnOpenAppClickListener(listener: () -> Unit) {
        this.onOpenAppClickListener = listener
    }

    fun setOnSizeChangedListener(listener: (width: Int, height: Int) -> Unit) {
        this.onSizeChangedListener = listener
    }

    fun setData(type: IslandType, title: String, text: String, items: List<String>) {
        this.currentType = type

        // Update collapsed view text
        collapsedText.text = title

        val badgeBg = collapsedBadge.background as GradientDrawable
        when (type) {
            IslandType.MONITORING -> {
                collapsedIcon.visibility = View.VISIBLE
                collapsedBadge.text = "监控中"
                collapsedBadge.setTextColor(Color.parseColor("#10B981")) // Emerald text
                badgeBg.setColor(Color.parseColor("#1A10B981")) // Emerald 10% alpha background

                expandedTitleText.text = "OpenInvest 后台监控中心"
                expandedProgressText.text = text
                progressTrackLayout.visibility = View.GONE
            }
            IslandType.ANALYZING -> {
                collapsedIcon.visibility = View.VISIBLE
                collapsedBadge.text = "分析中"
                collapsedBadge.setTextColor(Color.parseColor("#F59E0B")) // Amber text
                badgeBg.setColor(Color.parseColor("#1AF59E0B")) // Amber 10% alpha background

                expandedTitleText.text = "AI 智能多智能体评估"
                expandedProgressText.text = title
                progressTrackLayout.visibility = View.VISIBLE

                // Parse progress numbers like "(3/8)"
                val progress = parseProgress(text)
                if (progress != null) {
                    val completed = progress.first
                    val total = progress.second
                    updateWhalePosition(completed, total)
                } else {
                    updateWhalePosition(0, 1)
                }
            }
            IslandType.TRIGGER -> {
                // When trigger recommendation is active, hide the left whale icon
                collapsedIcon.visibility = View.GONE

                collapsedBadge.text = "交易警报"
                collapsedBadge.setTextColor(Color.parseColor("#EF4444")) // Rose Red text
                badgeBg.setColor(Color.parseColor("#1AEF4444")) // Rose Red 10% alpha background

                expandedTitleText.text = "交易决策信号触发"
                expandedProgressText.text = "信号触发列表（点击卡片进入应用）"
                progressTrackLayout.visibility = View.GONE
            }
        }

        // Update list items using structured reminder rows instead of log output
        detailsListLayout.removeAllViews()
        for (item in items) {
            val row = createReminderRow(context, item)
            detailsListLayout.addView(row)
        }
    }

    private fun createReminderRow(ctx: Context, item: String): View {
        val isBuy = item.contains("买") || item.contains("BUY")
        val isSell = item.contains("卖") || item.contains("SELL")

        val cleanItem = item.replace("^•\\s*".toRegex(), "").trim()

        // Extract title vs description if formatted like "宁德时代: 买入信号触发"
        val parts = cleanItem.split(":", limit = 2)
        val title = if (parts.size == 2) parts[0].trim() else "监控提醒"
        val desc = if (parts.size == 2) parts[1].trim() else cleanItem

        return LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dpToPx(6), 0, dpToPx(6))

            // 1. Color tag representing BUY or SELL
            val tagText = TextView(ctx).apply {
                text = if (isBuy) "买入" else if (isSell) "卖出" else "监控"
                setTextColor(Color.WHITE)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 10f)
                typeface = android.graphics.Typeface.create("sans-serif-bold", android.graphics.Typeface.BOLD)
                setPadding(dpToPx(7), dpToPx(2), dpToPx(7), dpToPx(2))

                val bg = GradientDrawable().apply {
                    setColor(Color.parseColor(if (isBuy) "#10B981" else if (isSell) "#EF4444" else "#3B82F6"))
                    cornerRadius = dpToPx(6).toFloat()
                }
                background = bg
            }
            val tagParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT)
            tagParams.rightMargin = dpToPx(10)
            addView(tagText, tagParams)

            // 2. Structured text (Stock Name in bold, details in slate color)
            val textContainer = LinearLayout(ctx).apply {
                orientation = LinearLayout.VERTICAL
                val p = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1.0f)
                layoutParams = p
            }

            val titleTv = TextView(ctx).apply {
                this.text = title
                setTextColor(Color.WHITE)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
                typeface = android.graphics.Typeface.create("sans-serif-bold", android.graphics.Typeface.BOLD)
            }
            textContainer.addView(titleTv)

            val descTv = TextView(ctx).apply {
                this.text = desc
                setTextColor(Color.parseColor("#94A3B8")) // Slate-400
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 10.5f)
                setLineSpacing(dpToPx(2).toFloat(), 1.0f)
            }
            textContainer.addView(descTv)

            addView(textContainer)
        }
    }

    private fun parseProgress(text: String): Pair<Int, Int>? {
        try {
            val parts = text.split("/")
            if (parts.size == 2) {
                val completed = parts[0].replace("[^0-9]".toRegex(), "").toInt()
                val total = parts[1].replace("[^0-9]".toRegex(), "").toInt()
                return Pair(completed, total)
            }
        } catch (e: Exception) {
            // ignore
        }
        return null
    }

    private fun updateSegments(completedIndex: Int, total: Int) {
        // Safe check N of segments
        if (segmentsContainer.childCount != total) {
            segmentsContainer.removeAllViews()
            for (i in 0 until total) {
                val segment = View(context).apply {
                    val bg = GradientDrawable().apply {
                        setColor(Color.parseColor("#27272A")) // Uncompleted gray background
                        cornerRadius = dpToPx(2).toFloat()
                    }
                    background = bg

                    val params = LinearLayout.LayoutParams(0, dpToPx(3), 1.0f)
                    if (i > 0) {
                        params.leftMargin = dpToPx(3) // 3dp gap between segments
                    }
                    layoutParams = params
                }
                segmentsContainer.addView(segment)
            }
        }

        // Color N segments dynamically:
        // - Completed segments (i < completedIndex): glowing indigo (#4F46E5)
        // - Current segment (i == completedIndex): glowing active blue (#3B82F6)
        // - Future segments (i > completedIndex): Zinc-800 dark gray (#27272A)
        for (i in 0 until total) {
            val segment = segmentsContainer.getChildAt(i)
            val bg = segment.background as GradientDrawable
            if (i < completedIndex) {
                bg.setColor(Color.parseColor("#4F46E5"))
            } else if (i == completedIndex) {
                bg.setColor(Color.parseColor("#3B82F6"))
            } else {
                bg.setColor(Color.parseColor("#27272A"))
            }
        }
    }

    private fun updateWhalePosition(completed: Int, total: Int) {
        val totalSafe = if (total <= 0) 1 else total
        // The index being evaluated (0-indexed)
        val activeIndex = (completed - 1).coerceIn(0, totalSafe - 1)

        // Render/update segments N horizontally
        updateSegments(activeIndex, totalSafe)

        progressTrackLayout.post {
            val trackWidth = progressTrackLayout.width
            val whaleWidth = progressWhale.width

            // Calculate center X of the active segment
            val ratio = (activeIndex + 0.5f) / totalSafe
            val targetX = (trackWidth * ratio - whaleWidth / 2f).coerceIn(0f, (trackWidth - whaleWidth).toFloat())

            val startX = progressWhale.translationX

            // 1. Horizontal movement animator
            val xAnimator = ValueAnimator.ofFloat(startX, targetX).apply {
                duration = 500
                interpolator = DecelerateInterpolator(1.1f)
                addUpdateListener { anim ->
                    progressWhale.translationX = anim.animatedValue as Float
                }
            }

            // 2. Vertical hopping (parabolic jump!) animator to make the whale "jump" onto the segment
            val yAnimator = ValueAnimator.ofFloat(0f, 1f).apply {
                duration = 500
                interpolator = DecelerateInterpolator()
                addUpdateListener { anim ->
                    val fraction = anim.animatedValue as Float
                    val hopHeight = dpToPx(12) // Jump height up to 12dp
                    // Parabolic arc formula: y = -4 * max_height * fraction * (1 - fraction)
                    progressWhale.translationY = -4 * hopHeight * fraction * (1 - fraction)
                }
            }

            xAnimator.start()
            yAnimator.start()
        }
    }

    private fun toggleExpand() {
        if (isExpanded) {
            collapse()
        } else {
            expand()
        }
    }

    fun expand() {
        if (isExpanded) return
        isExpanded = true

        val bg = background as GradientDrawable
        bg.cornerRadius = dpToPx(20).toFloat()

        // Fade out collapsed layout first
        collapsedLayout.animate()
            .alpha(0.0f)
            .setDuration(120)
            .setListener(object : AnimatorListenerAdapter() {
                override fun onAnimationEnd(animation: Animator) {
                    collapsedLayout.visibility = View.GONE
                    expandedLayout.visibility = View.VISIBLE
                    expandedLayout.alpha = 0.0f
                    // Measure the expanded layout dynamically based on content size
                    val widthSpec = MeasureSpec.makeMeasureSpec(expandedWidth, MeasureSpec.EXACTLY)
                    val heightSpec = MeasureSpec.makeMeasureSpec(0, MeasureSpec.UNSPECIFIED)
                    expandedLayout.measure(widthSpec, heightSpec)
                    val targetHeight = expandedLayout.measuredHeight

                    animateBounds(expandedWidth, targetHeight) {
                        expandedLayout.animate()
                            .alpha(1.0f)
                            .setDuration(150)
                            .setListener(null)
                            .start()
                    }
                }
            })
            .start()
    }

    fun collapse() {
        if (!isExpanded) return
        isExpanded = false

        val bg = background as GradientDrawable
        bg.cornerRadius = dpToPx(19).toFloat()

        // Fade out expanded layout first
        expandedLayout.animate()
            .alpha(0.0f)
            .setDuration(120)
            .setListener(object : AnimatorListenerAdapter() {
                override fun onAnimationEnd(animation: Animator) {
                    expandedLayout.visibility = View.GONE
                    collapsedLayout.visibility = View.VISIBLE
                    collapsedLayout.alpha = 0.0f

                    animateBounds(collapsedWidth, collapsedHeight) {
                        collapsedLayout.animate()
                            .alpha(1.0f)
                            .setDuration(150)
                            .setListener(null)
                            .start()
                    }
                }
            })
            .start()
    }

    private fun animateBounds(targetWidth: Int, targetHeight: Int, onComplete: () -> Unit) {
        val startWidth = width
        val startHeight = height

        val animator = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = 320
            interpolator = DecelerateInterpolator(1.2f)
            addUpdateListener { anim ->
                val fraction = anim.animatedValue as Float
                val currentW = (startWidth + (targetWidth - startWidth) * fraction).toInt()
                val currentH = (startHeight + (targetHeight - startHeight) * fraction).toInt()

                // Cast to standard ViewGroup.LayoutParams instead of ViewGroup.MarginLayoutParams
                // to prevent ClassCastException since top parent in WindowManager uses WindowManager.LayoutParams
                layoutParams = (layoutParams as ViewGroup.LayoutParams).apply {
                    width = currentW
                    height = currentH
                }

                // Let the WindowManager know to update window frame layout params in real-time
                onSizeChangedListener?.invoke(currentW, currentH)
            }
            addListener(object : AnimatorListenerAdapter() {
                override fun onAnimationEnd(animation: Animator) {
                    onComplete()
                }
            })
        }
        animator.start()
    }

    private fun dpToPx(dp: Int): Int {
        return TypedValue.applyDimension(
            TypedValue.COMPLEX_UNIT_DIP,
            dp.toFloat(),
            resources.displayMetrics
        ).toInt()
    }
}
